"""A live variable table for the wireless STM32 probe."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from live_memory import OpenOcd, display_value, load_symbols, manual_symbol, pack_value

POLL_MS = 300
READ_BUDGET = 0.22
FORMATS = {"uint": "Unsigned", "int": "Signed", "float": "Float", "hex": "Hex"}
RAM_TYPES = {"uint8": (1, "uint"), "int8": (1, "int"), "uint16": (2, "uint"), "int16": (2, "int"),
             "uint32": (4, "uint"), "int32": (4, "int"), "float32": (4, "float")}


class AddressDialog(simpledialog.Dialog):
    def body(self, panel):
        self.address = tk.StringVar()
        self.kind = tk.StringVar(value="uint32")
        self.name = tk.StringVar()
        ttk.Label(panel, text="Địa chỉ SRAM").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        entry = ttk.Entry(panel, textvariable=self.address, width=25)
        entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(panel, text="Kiểu").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(panel, textvariable=self.kind, values=list(RAM_TYPES), state="readonly").grid(row=1, column=1, sticky="ew")
        ttk.Label(panel, text="Tên tùy chọn").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(panel, textvariable=self.name).grid(row=2, column=1, sticky="ew", pady=4)
        self.result = None
        return entry

    def validate(self):
        try:
            self.result = manual_symbol(int(self.address.get().strip(), 0), *RAM_TYPES[self.kind.get()], self.name.get())
        except ValueError as error:
            messagebox.showerror("Địa chỉ không hợp lệ", str(error), parent=self)
            return False
        return True


class LiveWatch(ttk.Frame):
    def __init__(self, root, path, port):
        super().__init__(root, padding=12)
        self.root = root
        self.port = port
        self.watch = {}
        self.groups = {}
        self.paths = {}
        self.read_offset = 0
        self.available = {}
        self.raw_values = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending_read = None
        self.pending_write = None

        self.elf_text = tk.StringVar(value="Chọn ELF của firmware đang chạy")
        self.variable = tk.StringVar()
        self.status = tk.StringVar(value=f"OpenOCD Tcl · 127.0.0.1:{port}")

        files = ttk.Frame(self)
        ttk.Button(files, text="Mở ELF", command=self.choose_elf).pack(side="left")
        ttk.Label(files, textvariable=self.elf_text).pack(side="left", padx=8, fill="x", expand=True)
        files.pack(fill="x", pady=(0, 8))

        picker = ttk.Frame(self)
        self.combo = ttk.Combobox(picker, textvariable=self.variable)
        self.combo.pack(side="left", expand=True, fill="x")
        self.combo.bind("<KeyRelease>", self.filter_symbols)
        self.combo.bind("<Return>", lambda _event: self.add_variable())
        ttk.Button(picker, text="Thêm biến", command=self.add_variable).pack(side="left", padx=(8, 0))
        ttk.Button(picker, text="Thêm địa chỉ", command=self.add_address).pack(side="left", padx=(6, 0))
        picker.pack(fill="x", pady=(0, 8))

        table = ttk.Frame(self)
        self.rows = ttk.Treeview(table, columns=("type", "address", "value"), selectmode="browse")
        self.rows.heading("#0", text="Biến")
        self.rows.heading("type", text="Kiểu")
        self.rows.heading("address", text="Địa chỉ")
        self.rows.heading("value", text="Giá trị")
        self.rows.column("#0", width=270, minwidth=150)
        self.rows.column("type", width=100, minwidth=80, stretch=False)
        self.rows.column("address", width=115, minwidth=100, stretch=False)
        self.rows.column("value", width=130, minwidth=100)
        self.rows.bind("<<TreeviewSelect>>", lambda _event: self.update_selection())
        self.rows.bind("<Double-Button-1>", self.edit_row)
        self.rows.bind("<Return>", lambda _event: self.edit_value())
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.rows.yview)
        self.rows.configure(yscrollcommand=scrollbar.set)
        self.rows.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)

        actions = ttk.Frame(self)
        self.format = tk.StringVar()
        self.format_box = ttk.Combobox(actions, textvariable=self.format, width=12, state="disabled")
        self.format_box.pack(side="left")
        self.format_box.bind("<<ComboboxSelected>>", self.change_format)
        self.edit_button = ttk.Button(actions, text="Ghi giá trị", command=self.edit_value, state="disabled")
        self.edit_button.pack(side="left", padx=8)
        self.remove_button = ttk.Button(actions, text="Bỏ theo dõi", command=self.remove_variable, state="disabled")
        self.remove_button.pack(side="left")
        actions.pack(fill="x", pady=8)
        ttk.Label(self, textvariable=self.status, wraplength=680).pack(fill="x")

        self.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if path:
            self.open_elf(path)
        self.root.after_idle(self.poll)

    def can_use_target(self):
        return True

    def choose_elf(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("ELF", "*.elf"), ("All files", "*")])
        if path:
            self.open_elf(path)

    def open_elf(self, path):
        if self.pending_write is not None:
            return
        try:
            symbols = self.read_symbols(path)
        except Exception as error:
            messagebox.showerror("Không mở được ELF", str(error), parent=self.root)
            return
        self.available = {item.label: item for item in symbols}
        self.clear_watch()
        self.update_selection()
        self.variable.set("")
        self.combo["values"] = list(self.available)
        self.elf_text.set(path)
        self.status.set(f"{len(symbols)} biến RAM · Mở mũi tên để xem các trường của struct.")
        return True

    def read_symbols(self, path):
        return load_symbols(path)

    def clear_watch(self):
        for records in (self.watch, self.groups, self.paths, self.raw_values):
            records.clear()
        self.read_offset = 0
        self.rows.delete(*self.rows.get_children())

    def add_address(self):
        dialog = AddressDialog(self.root, "Watch theo địa chỉ")
        if dialog.result is not None:
            self.add_symbol(dialog.result)

    def filter_symbols(self, event):
        if event.keysym not in ("Up", "Down", "Return", "Escape", "Tab"):
            query = self.variable.get().casefold()
            self.combo["values"] = [name for name in self.available if query in name.casefold()]

    def add_variable(self):
        query = self.variable.get()
        symbol = self.available.get(query)
        if symbol is None:
            matches = [item for item in self.available.values() if item.name == query]
            if len(matches) == 1:
                symbol = matches[0]
        if symbol is None:
            self.status.set("Chọn một biến RAM từ danh sách.")
            return

        self.add_symbol(symbol)

    def add_symbol(self, symbol, kind=None):
        for row in self.rows.get_children():
            if self.root_symbol(row) == symbol:
                self.rows.selection_set(row)
                return row
        row = self.insert_node("", symbol, symbol.name)
        if symbol.is_group:
            self.rows.item(row, open=True)
        elif kind is not None:
            self.set_format(row, kind)
        self.rows.selection_set(row)
        self.rows.focus(row)
        self.update_selection()
        return row

    def root_symbol(self, row):
        return self.groups[row] if row in self.groups else self.watch[row][0]

    def insert_node(self, parent, symbol, path):
        if symbol.is_group:
            kind = symbol.type_name or "Struct"
            value = "" if symbol.members else "Không có trường hỗ trợ"
        else:
            kind = FORMATS[symbol.kind] if symbol.writable else f"{FORMATS[symbol.kind]} · chỉ đọc"
            value = "—"
        row = self.rows.insert(parent, "end", text=symbol.name, values=(kind, f"0x{symbol.address:08X}", value))
        self.paths[row] = path
        if symbol.is_group:
            self.groups[row] = symbol
            for member in symbol.members:
                separator = "" if member.name.startswith("[") else "."
                self.insert_node(row, member, path + separator + member.name)
        else:
            self.watch[row] = (symbol, symbol.kind)
        return row

    def descendants(self, parent, *, expanded_only=False):
        if expanded_only and parent and not self.rows.item(parent, "open"):
            return
        for row in self.rows.get_children(parent):
            yield row
            if row in self.groups:
                yield from self.descendants(row, expanded_only=expanded_only)

    def update_selection(self):
        selected = self.rows.selection()
        row = selected[0] if selected else None
        if row not in self.watch:
            self.format.set("")
            self.format_box.configure(state="disabled")
            self.edit_button.configure(state="disabled")
            self.remove_button.configure(state="normal" if row in self.groups and not self.rows.parent(row) else "disabled")
            return
        symbol, kind = self.watch[row]
        self.format_box.configure(values=[FORMATS[item] for item in (*symbol.formats, "hex")], state="readonly")
        self.format.set(FORMATS[kind])
        writable = symbol.writable and self.pending_write is None and self.can_use_target()
        self.edit_button.configure(state="normal" if writable else "disabled")
        self.remove_button.configure(state="disabled" if self.rows.parent(row) else "normal")

    def change_format(self, _event):
        selected = self.rows.selection()
        if not selected or selected[0] not in self.watch:
            return
        kind = next(key for key, label in FORMATS.items() if label == self.format.get())
        self.set_format(selected[0], kind)

    def set_format(self, row, kind):
        symbol, _ = self.watch[row]
        if kind not in (*symbol.formats, "hex"):
            return
        self.watch[row] = (symbol, kind)
        self.rows.set(row, "type", FORMATS[kind] if symbol.writable else f"{FORMATS[kind]} · chỉ đọc")
        if row in self.raw_values:
            self.show_value(row, self.raw_values[row])

    def remove_variable(self):
        selected = self.rows.selection()
        if not selected or self.rows.parent(selected[0]):
            return
        row = selected[0]
        for child in (row, *self.descendants(row)):
            for records in (self.watch, self.groups, self.paths, self.raw_values):
                records.pop(child, None)
        self.rows.delete(row)
        self.update_selection()

    def edit_row(self, event):
        row = self.rows.identify_row(event.y)
        if row in self.watch and self.rows.identify_column(event.x) == "#3":
            self.rows.selection_set(row)
            self.edit_value()

    def edit_value(self):
        selected = self.rows.selection()
        if (not selected or selected[0] not in self.watch or self.pending_write is not None
                or not self.can_use_target()):
            return
        row = selected[0]
        symbol, kind = self.watch[row]
        if not symbol.writable:
            return
        session = self.write_session()
        value = simpledialog.askstring("Ghi giá trị", f"{self.paths[row]} (0x{symbol.address:08X})", parent=self.root)
        if value is None or self.watch.get(row) != (symbol, kind) or not self.same_session(session):
            return
        try:
            raw = pack_value(value, symbol.size, kind)
        except (ValueError, OverflowError) as error:
            messagebox.showerror("Giá trị không hợp lệ", str(error), parent=self.root)
            return
        self.pending_write = (row, self.executor.submit(self.write, symbol, raw), raw)
        self.update_selection()

    def write_session(self):
        return None

    def same_session(self, session):
        return self.can_use_target() and self.write_session() is session

    def read_all(self, rows):
        values = {}
        failed = {}
        deadline = monotonic() + READ_BUDGET
        with OpenOcd(self.port) as ocd:
            for row, (symbol, _) in rows:
                try:
                    values[row] = ocd.read(symbol)
                except (RuntimeError, ValueError) as error:
                    failed[row] = error
                if monotonic() >= deadline:
                    break
        return values, failed

    def write(self, symbol, raw):
        with OpenOcd(self.port) as ocd:
            return ocd.write(symbol, raw)

    def show_value(self, row, raw):
        entry = self.watch.get(row)
        if entry is not None:
            symbol, kind = entry
            self.raw_values[row] = raw
            self.rows.set(row, "value", display_value(raw, symbol.size, kind))

    def on_read(self, values):
        pass

    def on_read_error(self, error):
        pass

    def on_write_error(self, error):
        pass

    def poll(self):
        if self.pending_read is not None and self.pending_read.done():
            try:
                values, failed = self.pending_read.result()
                self.read_offset += len(values) + len(failed)
                for row, raw in values.items():
                    self.show_value(row, raw)
                for row in failed:
                    if row in self.watch:
                        self.raw_values.pop(row, None)
                        self.rows.set(row, "value", "Lỗi đọc")
                self.on_read(values)
                if failed and self.pending_write is None:
                    self.status.set(f"Có {len(failed)} biến lỗi đọc. Kiểm tra địa chỉ và độ rộng.")
                elif self.pending_write is None and not self.status.get().startswith("Đã ghi "):
                    self.status.set(f"Đang theo dõi · 127.0.0.1:{self.port}")
            except (OSError, ValueError, RuntimeError) as error:
                self.raw_values.clear()
                for row in self.watch:
                    self.rows.set(row, "value", "—")
                self.status.set(f"Không đọc được qua Tcl {self.port} · {error}")
                self.on_read_error(error)
            self.pending_read = None

        if self.pending_write is not None:
            row, task, expected = self.pending_write
            if task.done():
                self.pending_write = None
                try:
                    raw = task.result()
                    self.show_value(row, raw)
                    if raw == expected:
                        self.status.set("Đã ghi và đọc lại giá trị qua SWD.")
                    elif row in self.watch:
                        symbol, kind = self.watch[row]
                        requested = display_value(expected, symbol.size, kind)
                        observed = display_value(raw, symbol.size, kind)
                        self.status.set(f"Đã ghi {requested}, đọc lại {observed}.")
                except (OSError, ValueError, RuntimeError) as error:
                    self.status.set(f"Lỗi ghi hoặc đọc lại · {error}")
                    self.on_write_error(error)
                self.update_selection()
        elif self.watch and self.pending_read is None and self.can_use_target():
            active = [(row, self.watch[row]) for row in self.descendants("", expanded_only=True) if row in self.watch]
            if active:
                offset = self.read_offset % len(active)
                self.pending_read = self.executor.submit(self.read_all, active[offset:] + active[:offset])

        self.root.after(POLL_MS, self.poll)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Read and write STM32 variables through OpenOCD.")
    parser.add_argument("elf", nargs="?")
    parser.add_argument("--tcl-port", type=int, default=6666)
    args = parser.parse_args()

    root = tk.Tk()
    root.title("STM32 Live Watch")
    root.geometry("730x430")
    root.minsize(600, 320)
    LiveWatch(root, args.elf, args.tcl_port)
    root.mainloop()


if __name__ == "__main__":
    main()
