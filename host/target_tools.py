"""Verify STM32 flash without stopping the core and capture SEGGER RTT output."""

import codecs
import socket
import struct
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from live_memory import OpenOcd

FLASH_START = 0x08000000
FLASH_END = 0x10000000


class FirmwareMismatch(RuntimeError):
    def __init__(self, address, expected, actual):
        self.address = address
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Flash khác ELF tại 0x{address:08X}: ELF 0x{expected:02X}, STM32 0x{actual:02X}."
        )


def _arm_elf(stream):
    elf = ELFFile(stream)
    if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_ARM" or elf["e_type"] != "ET_EXEC":
        raise ValueError("Chọn ELF ARM 32-bit đã link cho STM32.")
    return elf


def verify_elf(path, link, *, cancelled=None, progress=None):
    """Compare file-backed internal flash bytes and return the number read."""
    if cancelled is not None and cancelled():
        raise InterruptedError("Đã hủy đối chiếu ELF.")
    regions = []
    with Path(path).open("rb") as stream:
        for segment in _arm_elf(stream).iter_segments(type="PT_LOAD"):
            address = segment["p_paddr"]
            size = segment["p_filesz"]
            if not size or not FLASH_START <= address < FLASH_END:
                continue
            if size > segment["p_memsz"] or address + size > FLASH_END:
                raise ValueError("ELF có vùng nạp flash không hợp lệ.")
            contents = segment.data()
            if len(contents) != size:
                raise ValueError("ELF bị thiếu dữ liệu vùng nạp flash.")
            regions.append((address, contents))
    total = sum(len(contents) for _, contents in regions)
    if not total:
        raise ValueError("ELF không có dữ liệu nạp trong flash nội STM32.")

    done = 0
    if progress is not None:
        progress(done, total)
    for base, contents in regions:
        offset = 0
        while offset < len(contents):
            if cancelled is not None and cancelled():
                raise InterruptedError("Đã hủy đối chiếu ELF.")
            address = base + offset
            remaining = len(contents) - offset
            if address % 4 or remaining < 4:
                width = 1
                count = min(remaining, 4 - address % 4)
            else:
                width = 4
                count = min(128, remaining // width)
            response = link._call(f"read_memory 0x{address:08X} {width * 8} {count}")
            words = response.split()
            if len(words) != count:
                raise RuntimeError(f"OpenOCD trả thiếu dữ liệu flash tại 0x{address:08X}.")
            try:
                actual = struct.pack(f"<{count}{'I' if width == 4 else 'B'}", *(int(word, 0) for word in words))
            except (ValueError, struct.error) as error:
                raise RuntimeError(f"OpenOCD trả dữ liệu flash không hợp lệ tại 0x{address:08X}.") from error
            expected = contents[offset : offset + len(actual)]
            if actual != expected:
                for index, (got, want) in enumerate(zip(actual, expected)):
                    if got != want:
                        raise FirmwareMismatch(address + index, want, got)
            offset += len(actual)
            done += len(actual)
            if progress is not None:
                progress(done, total)
    return done


def rtt_address(path):
    """Find the linked SEGGER RTT control block in STM32 SRAM."""
    with Path(path).open("rb") as stream:
        table = _arm_elf(stream).get_section_by_name(".symtab")
        if isinstance(table, SymbolTableSection):
            for symbol in table.get_symbol_by_name("_SEGGER_RTT") or ():
                address = symbol["st_value"]
                if 0x20000000 <= address <= 0x40000000 - 24 and address % 4 == 0:
                    return address
    return None


class RttCapture:
    """Read RTT from the app's loopback-bound OpenOCD instance."""

    def __init__(self, address, *, size=16, channel=0, tcl_port=6666):
        if not isinstance(size, int) or size < 16 or not isinstance(address, int) or not (
            0x20000000 <= address <= 0x40000000 - max(size, 24)
        ):
            raise ValueError("Địa chỉ hoặc vùng tìm RTT trong SRAM không hợp lệ.")
        if not isinstance(channel, int) or not 0 <= channel <= 255:
            raise ValueError("Kênh RTT phải trong khoảng 0–255.")
        self.address = address
        self.size = size
        self.channel = channel
        self.tcl_port = tcl_port
        self._port = None
        self._started = False
        self._client = None
        self._decoder = None

    def start(self):
        if self._started:
            raise RuntimeError("RTT đang chạy.")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        try:
            with OpenOcd(port=self.tcl_port) as link:
                link._call(f"rtt setup 0x{self.address:08X} {self.size}")
                link._call("rtt start")
                self._started = True
                link._call(f"rtt server start {port} {self.channel}")
                self._port = port
            self._client = socket.create_connection(("127.0.0.1", port), timeout=1.5)
            self._client.settimeout(0.05)
            self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        except (RuntimeError, OSError):
            try:
                self.stop()
            except (RuntimeError, OSError):
                pass
            raise

    def read(self):
        client, decoder = self._client, self._decoder
        if client is None or decoder is None:
            raise ConnectionError("RTT chưa kết nối.")
        try:
            data = client.recv(4096)
        except socket.timeout:
            return ""
        if not data:
            raise ConnectionError("OpenOCD đã đóng kết nối RTT.")
        return decoder.decode(data)

    def stop(self):
        client, port, started = self._client, self._port, self._started
        self._client = self._port = self._decoder = None
        self._started = False
        if client is not None:
            client.close()
        if started:
            with OpenOcd(port=self.tcl_port) as link:
                try:
                    if port is not None:
                        link._call(f"rtt server stop {port}")
                finally:
                    link._call("rtt stop")
