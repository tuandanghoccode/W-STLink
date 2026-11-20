"""Read and write Cortex-M SRAM symbols through a local OpenOCD server."""

from dataclasses import dataclass
from pathlib import Path
import math
import socket
import struct

from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

TCL_END = b"\x1a"
CHUNK_SIZE = 4096


@dataclass(frozen=True)
class Symbol:
    name: str
    address: int
    size: int
    kind: str
    manual: bool = False
    type_name: str = ""
    members: tuple["Symbol", ...] = ()
    writable: bool = True

    @property
    def label(self):
        return f"{self.name}  (0x{self.address:08X})"

    @property
    def is_group(self):
        return self.kind == "group"

    @property
    def formats(self):
        if self.is_group:
            return ()
        return ("uint", "int", "float") if self.size == 4 else ("uint", "int")


def manual_symbol(address, size, kind, name=""):
    if (type(size) is not int or size not in (1, 2, 4) or kind not in ("uint", "int", "float")
            or (kind == "float" and size != 4) or not isinstance(name, str)):
        raise ValueError("Kiểu biến không hợp lệ.")
    if type(address) is not int or not 0x20000000 <= address <= 0x40000000 - size or address % size:
        raise ValueError("Nhập địa chỉ SRAM và căn hàng theo kích thước biến.")
    return Symbol(name.strip() or f"RAM_0x{address:08X}", address, size, kind, manual=True)


def numeric_value(raw, size, kind):
    data = raw.to_bytes(size, "little")
    if kind == "float":
        return struct.unpack("<f", data)[0]
    return int.from_bytes(data, "little", signed=kind == "int")


TYPE_QUALIFIERS = {
    "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
    "DW_TAG_restrict_type", "DW_TAG_atomic_type",
}
GROUP_NAMES = {
    "DW_TAG_structure_type": "struct", "DW_TAG_union_type": "union",
    "DW_TAG_class_type": "class", "DW_TAG_array_type": "array",
}
CONSTANT_FORMS = {
    "DW_FORM_data1", "DW_FORM_data2", "DW_FORM_data4", "DW_FORM_data8",
    "DW_FORM_sdata", "DW_FORM_udata", "DW_FORM_implicit_const",
}
EXPRESSION_FORMS = {"DW_FORM_exprloc", "DW_FORM_block", "DW_FORM_block1", "DW_FORM_block2", "DW_FORM_block4"}
MAX_DEPTH = 12
MAX_ARRAY_ITEMS = 32
MAX_SYMBOL_NODES = 256


def _constant(die, key, default=None):
    attribute = die.attributes.get(key)
    if attribute is None:
        return default
    return attribute.value if attribute.form in CONSTANT_FORMS else None


def _type_reference(die, key="DW_AT_type"):
    return die.get_DIE_from_attribute(key) if key in die.attributes else None


def _name(die, key="DW_AT_name"):
    attribute = die.attributes.get(key)
    return attribute.value.decode("utf-8", errors="replace") if attribute is not None else ""


def _unwrap(die):
    name = ""
    readonly = False
    for _ in range(MAX_DEPTH):
        if die is None:
            break
        name = name or _name(die)
        if die.tag not in TYPE_QUALIFIERS:
            return die, name, readonly
        if die.tag in {"DW_TAG_const_type", "DW_TAG_atomic_type"}:
            readonly = True
        die = _type_reference(die)
    return None, name, readonly


def _dwarf_variables(elf):
    if not elf.has_dwarf_info(strict=True):
        return {}, None
    dwarf = elf.get_dwarf_info()
    expressions = DWARFExprParser(dwarf.structs)
    variables = {}
    for unit in dwarf.iter_CUs():
        for die in unit.iter_DIEs():
            location = die.attributes.get("DW_AT_location")
            if die.tag != "DW_TAG_variable" or location is None or location.form not in EXPRESSION_FORMS:
                continue
            operations = expressions.parse_expr(location.value)
            if len(operations) != 1 or operations[0].op_name != "DW_OP_addr":
                continue
            address = operations[0].args[0]
            if not 0x20000000 <= address < 0x40000000:
                continue

            declaration = die
            for _ in range(MAX_DEPTH):
                kind = _type_reference(declaration)
                if kind is not None:
                    names = variables.setdefault(address, {})
                    name = _name(die, "DW_AT_linkage_name") or _name(declaration, "DW_AT_linkage_name")
                    names[name or _name(die) or _name(declaration)] = kind
                    break
                declaration = (_type_reference(declaration, "DW_AT_specification")
                               or _type_reference(declaration, "DW_AT_abstract_origin"))
                if declaration is None:
                    break
    return variables, expressions


def _array_layout(die, depth):
    if depth >= MAX_DEPTH or _constant(die, "DW_AT_ordering", 0) != 0 or "DW_AT_bit_stride" in die.attributes:
        return None
    element = _type_reference(die)
    base, _, _ = _unwrap(element)
    if base is None:
        return None
    element_size = _constant(base, "DW_AT_byte_size")
    if element_size is None and base.tag == "DW_TAG_array_type":
        nested = _array_layout(base, depth + 1)
        if nested is not None:
            _, ranges = nested
            element_size = ranges[0][1] * ranges[0][2]
    if not element_size or _constant(die, "DW_AT_byte_stride", element_size) != element_size:
        return None

    ranges = []
    for subrange in die.iter_children():
        if subrange.tag != "DW_TAG_subrange_type" or len(ranges) >= MAX_DEPTH:
            return None
        lower = _constant(subrange, "DW_AT_lower_bound", 0)
        count = _constant(subrange, "DW_AT_count")
        if count is None and "DW_AT_count" not in subrange.attributes:
            upper = _constant(subrange, "DW_AT_upper_bound")
            if lower is not None and upper is not None:
                count = upper - lower + 1
        if lower is None or count is None or not 0 < count <= 0x20000000:
            return None
        ranges.append((lower, count))
    if not ranges:
        return None

    layout = []
    stride = element_size
    for lower, count in reversed(ranges):
        layout.append((lower, count, stride))
        stride *= count
        if stride > 0x20000000:
            return None
    return element, tuple(reversed(layout))


def _member_offset(member, expressions, union):
    if any(key in member.attributes for key in ("DW_AT_bit_size", "DW_AT_bit_offset", "DW_AT_data_bit_offset")):
        return None
    location = member.attributes.get("DW_AT_data_member_location")
    if location is None:
        return 0 if union else None
    if location.form in EXPRESSION_FORMS:
        operations = expressions.parse_expr(location.value)
        if len(operations) == 1 and operations[0].op_name == "DW_OP_plus_uconst":
            return operations[0].args[0]
    return _constant(member, "DW_AT_data_member_location")


def _scalar_kind(die, size):
    encoding = _constant(die, "DW_AT_encoding")
    if die.tag == "DW_TAG_enumeration_type" and encoding is None:
        base, _, _ = _unwrap(_type_reference(die))
        if base is not None:
            encoding = _constant(base, "DW_AT_encoding")
        else:
            values = [_constant(item, "DW_AT_const_value") for item in die.iter_children()
                      if item.tag == "DW_TAG_enumerator"]
            if values and all(value is not None for value in values):
                encoding = 5 if min(values) < 0 else 7
    if encoding in (5, 6):
        return "int"
    if encoding in (2, 7, 8):
        return "uint"
    if encoding == 4 and size == 4:
        return "float"
    return None


def _array_fields(layout, address, expressions, budget, readonly, depth):
    element, dimensions = layout
    lower, count, stride = dimensions[0]
    members = []
    for index in range(min(count, MAX_ARRAY_ITEMS)):
        if budget[0] <= 0 or depth >= MAX_DEPTH:
            break
        start = address + index * stride
        name = f"[{lower + index}]"
        if len(dimensions) > 1:
            budget[0] -= 1
            children = _array_fields((element, dimensions[1:]), start, expressions, budget, readonly, depth + 1)
            member = Symbol(name, start, stride, "group", type_name="array", members=children, writable=False)
        else:
            member = _typed_symbol(name, element, start, start + stride, expressions, budget, readonly, depth)
        if member is not None:
            members.append(member)
    return tuple(members)


def _typed_symbol(name, die, address, limit, expressions, budget, readonly=False, depth=0, known_size=None):
    if budget[0] <= 0 or depth >= MAX_DEPTH:
        return None
    die, type_name, inherited_readonly = _unwrap(die)
    if die is None:
        return None
    readonly = readonly or inherited_readonly
    tag = die.tag
    size = _constant(die, "DW_AT_byte_size")
    layout = _array_layout(die, depth) if tag == "DW_TAG_array_type" else None
    if layout is not None:
        _, (first, *_) = layout
        expected = first[1] * first[2]
        if size is not None and size != expected:
            return None
        size = expected
    if size is None:
        size = known_size
    if not size or address + size > limit or (known_size is not None and size != known_size):
        return None

    if tag in GROUP_NAMES:
        budget[0] -= 1
        end = address + size
        members = []
        if tag == "DW_TAG_array_type":
            if layout is not None:
                members = _array_fields(layout, address, expressions, budget, readonly, depth + 1)
        else:
            union = tag == "DW_TAG_union_type"
            for field in die.iter_children():
                if budget[0] <= 0:
                    break
                if field.tag != "DW_TAG_member":
                    continue
                offset = _member_offset(field, expressions, union)
                if offset is None or not 0 <= offset < size:
                    continue
                field_name = _name(field) or f"<+0x{offset:X}>"
                member = _typed_symbol(field_name, _type_reference(field), address + offset, end, expressions,
                                       budget, readonly or union, depth + 1)
                if member is not None:
                    members.append(member)
        return Symbol(name, address, size, "group", type_name=type_name or GROUP_NAMES[tag],
                      members=tuple(members), writable=False)

    if tag in {"DW_TAG_base_type", "DW_TAG_enumeration_type"} and size in (1, 2, 4):
        kind = _scalar_kind(die, size)
        if kind is not None:
            budget[0] -= 1
            return Symbol(name, address, size, kind, type_name=type_name, writable=not readonly and address % size == 0)
    return None


def load_symbols(path):
    with Path(path).open("rb") as stream:
        elf = ELFFile(stream)
        if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_ARM" or elf["e_type"] != "ET_EXEC":
            raise ValueError("Chọn ELF ARM 32-bit đã link cho STM32.")
        table = elf.get_section_by_name(".symtab")
        if not isinstance(table, SymbolTableSection):
            raise ValueError("ELF thiếu bảng symbol. Dùng ELF từ build Debug.")

        variables, expressions = _dwarf_variables(elf)
        symbols = set()
        for entry in table.iter_symbols():
            index = entry["st_shndx"]
            size = entry["st_size"]
            address = entry["st_value"]
            if (
                entry["st_info"]["type"] != "STT_OBJECT"
                or not entry.name
                or not isinstance(index, int)
                or not size
                or not 0x20000000 <= address <= 0x40000000 - size
            ):
                continue

            section = elf.get_section(index)
            if section["sh_flags"] & 3 != 3:  # SHF_WRITE | SHF_ALLOC
                continue

            types = variables.get(address)
            if types:
                die = types.get(entry.name)
                if die is None and len(types) == 1:
                    die = next(iter(types.values()))
                if die is not None:
                    symbol = _typed_symbol(entry.name, die, address, address + size, expressions,
                                           [MAX_SYMBOL_NODES], known_size=size)
                    if symbol is not None:
                        symbols.add(symbol)
                continue

            if size in (1, 2, 4) and address % size == 0:
                symbols.add(Symbol(entry.name, address, size, "uint"))

    return sorted(symbols, key=lambda item: (item.name.casefold(), item.address))


def pack_value(text, size, kind):
    if kind == "float" and size == 4:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("Nhập một số thực hữu hạn.")
        return int.from_bytes(struct.pack("<f", value), "little")
    if kind not in ("int", "uint", "hex"):
        raise ValueError("Kiểu dữ liệu chưa được hỗ trợ.")

    # Treat numbers without a prefix as decimal, including a leading zero.
    prefix = text.lstrip().lstrip("+-").lower()[:2]
    value = int(text, 0 if prefix in ("0x", "0o", "0b") else 10)
    if kind == "int" and prefix in ("0x", "0o", "0b") and value >= 0:
        return int.from_bytes(value.to_bytes(size, "little"), "little")
    return int.from_bytes(value.to_bytes(size, "little", signed=kind == "int"), "little")


def display_value(raw, size, kind):
    data = raw.to_bytes(size, "little")
    if kind == "float":
        return f"{struct.unpack('<f', data)[0]:.9g}"
    if kind == "hex":
        return f"0x{raw:0{size * 2}X}"
    return str(int.from_bytes(data, "little", signed=kind == "int"))


class OpenOcd:
    def __init__(self, port=6666, timeout=1.5):
        self.port = port
        self.timeout = timeout
        self.connection = None

    def __enter__(self):
        self.connection = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        return self

    def __exit__(self, *_):
        self.connection.close()

    def _call(self, command):
        wrapper = 'if {[catch {' + command + '} result]} {format "ERR %s" $result} else {format "OK %s" $result}'
        self.connection.sendall(wrapper.encode("utf-8") + TCL_END)
        response = bytearray()
        while TCL_END not in response:
            data = self.connection.recv(CHUNK_SIZE)
            if not data:
                raise ConnectionError("OpenOCD đã đóng kết nối.")
            response.extend(data)
            if len(response) > CHUNK_SIZE:
                raise RuntimeError("Phản hồi OpenOCD quá dài.")

        status, separator, result = response.partition(TCL_END)[0].decode("utf-8", errors="replace").partition(" ")
        if not separator or status != "OK":
            raise RuntimeError(result or "Phản hồi OpenOCD không hợp lệ.")
        return result.strip()

    def read(self, symbol):
        if symbol.is_group or symbol.size not in (1, 2, 4):
            raise ValueError("Chọn một trường có thể đọc.")
        if symbol.address % symbol.size == 0:
            return int(self._call(f"read_memory 0x{symbol.address:x} {symbol.size * 8} 1"), 0)

        values = self._call(f"read_memory 0x{symbol.address:x} 8 {symbol.size}").split()
        if len(values) != symbol.size:
            raise RuntimeError("OpenOCD trả thiếu dữ liệu trường.")
        return int.from_bytes(bytes(int(value, 0) for value in values), "little")

    def write(self, symbol, raw):
        if symbol.is_group or not symbol.writable or symbol.size not in (1, 2, 4) or symbol.address % symbol.size:
            raise ValueError("Trường này chỉ đọc.")
        if not 0 <= raw < 1 << (8 * symbol.size):
            raise ValueError("Giá trị vượt phạm vi của biến.")
        self._call(f"write_memory 0x{symbol.address:x} {symbol.size * 8} {{0x{raw:x}}}")
        return self.read(symbol)
