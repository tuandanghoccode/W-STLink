import atexit

"""Standalone Windows app for the wireless STM32 probe."""

import os
import tkinter as tk
from ipaddress import IPv4Address
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event
from time import monotonic
from tkinter import messagebox, simpledialog, ttk

from ble_bridge import BleBridge, configure_wifi_ble, discover_ble_probes
from live_memory import OpenOcd, display_value, manual_symbol, numeric_value
from live_watch import LiveWatch
from plot_window import PlotWindow
from probe_server import (
    TARGETS,
    ProbeServer,
    configure_wifi,
    discover_wifi,
    get_usb_mode,
    set_usb_mode,
    usb_ip,
    usb_probes,
)
from robotics_tab import RoboticsTab
from rtt_window import RttWindow
from target_tools import RttCapture, rtt_address, verify_elf
from watch_profiles import ProfileStore

TRANSPORTS = ("Wi-Fi", "Bluetooth")
CHECK_INTERVAL = 4
RETRY_INTERVAL = 6
LINK_STEPS = (
    ("probe", "Chuẩn bị ESP32"),
    ("openocd", "Mở OpenOCD"),
    ("swd", "Kiểm tra SWD"),
)
CORE_LABELS = {
    "running": "Đang chạy",
    "debug-running": "Đang chạy debug",
    "halted": "Đã dừng",
    "reset": "Đang reset",
    "unavailable": "Chưa truy cập được",
    "unknown": "Chưa rõ trạng thái",
}


class PresetDialog(simpledialog.Dialog):
    def __init__(self, parent, options):
        self.options = options
        super().__init__(parent, "Lưu giá trị tuning")

    def body(self, panel):
        self.name = ttk.Entry(panel, width=32)
        self.name.pack(fill="x", padx=8, pady=6)
        ttk.Label(panel, text="Chọn các biến cần lưu").pack(anchor="w", padx=8)
        container = ttk.Frame(panel)
        container.pack(fill="both", expand=True, padx=8, pady=6)
        self.variables = tk.Listbox(
            container,
            selectmode="extended",
            height=min(12, len(self.options)),
            width=55,
            exportselection=False,
        )
        scrollbar = ttk.Scrollbar(container, command=self.variables.yview)
        self.variables.configure(yscrollcommand=scrollbar.set)
        self.variables.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for path, symbol, kind, raw in self.options:
            value = display_value(raw, symbol.size, kind)
            self.variables.insert("end", f"{path} (0x{symbol.address:08X})  =  {value}")
        return self.name

    def validate(self):
        name = self.name.get().strip()
        selected = self.variables.curselection()
        if not name or not selected:
            messagebox.showerror(
                "Thiếu thông tin", "Nhập tên và chọn ít nhất một biến.", parent=self
            )
            return False
        self.result = (
            name,
            [
                {
                    "name": symbol.label,
                    "manual": symbol.manual,
                    "address": symbol.address,
                    "size": symbol.size,
                    "kind": symbol.kind,
                    "raw": raw,
                }
                for _, symbol, _, raw in (self.options[index] for index in selected)
            ],
        )
        return True


class DesktopApp(LiveWatch):
    def __init__(self, root, folder):
        self.profiles = ProfileStore(folder / "settings.json")
        self.profile = self.profiles.active
        self.server = ProbeServer(folder / "openocd.log")
        self.activity = self.health = None
        self.elf_path = self.verified = self.elf_version = None
        self.plot = self.rtt = self.rtt_window = self.bridge = None
        self.verify_cancel = Event()
        self.verify_progress = None
        self.link_status = ""
        self.link_step = -1
        self.link_events = SimpleQueue()
        self.core_state = None
        self.keep_link = self.closing = False
        self.next_check = self.next_retry = 0.0
        self.wifi_ip = ""
        self.ble_address = ""

        super().__init__(root, None, 6666, defer_ui=True)
        self.connection_text = tk.StringVar(value="Chọn cách kết nối ESP32.")
        self.profile_name = tk.StringVar(value=self.profile)
        self.transport = tk.StringVar(value=TRANSPORTS[0])
        self.ip = tk.StringVar()
        self.target = tk.StringVar(value="STM32F4")
        self.core_text = tk.StringVar(value="STM32 · Chưa kết nối")
        self.link_detail = tk.StringVar(value="Chọn Wi-Fi hoặc Bluetooth rồi kết nối.")

        profiles = ttk.Frame(self)
        profiles.pack(fill="x", pady=(0, 6))
        ttk.Label(profiles, text="Profile").pack(side="left")
        self.profile_box = ttk.Combobox(
            profiles,
            textvariable=self.profile_name,
            values=list(self.profiles.profiles),
            width=22,
            state="readonly",
        )
        self.profile_box.pack(side="left", padx=6)
        self.profile_box.bind("<<ComboboxSelected>>", self.change_profile)
        self.profile_button = self.button(
            profiles, "Lưu profile mới", self.save_profile_as
        )
        self.connection_label = ttk.Label(
            profiles, textvariable=self.connection_text, wraplength=580
        )
        self.connection_label.pack(side="left", padx=8, fill="x", expand=True)

        connection = ttk.Frame(self)
        connection.pack(fill="x", pady=(0, 6))
        self.transport_box = ttk.Combobox(
            connection,
            textvariable=self.transport,
            values=TRANSPORTS,
            width=10,
            state="readonly",
        )
        self.transport_box.pack(side="left")
        self.transport_box.bind("<<ComboboxSelected>>", self._on_transport_changed)
        self.ip_entry = ttk.Entry(connection, textvariable=self.ip, width=22)
        self.ip_entry.pack(side="left", padx=6)
        self.ip_entry.bind("<Return>", lambda _: self.toggle_connection())
        self.target_box = ttk.Combobox(
            connection,
            textvariable=self.target,
            values=list(TARGETS),
            width=10,
            state="readonly",
        )
        self.target_box.pack(side="left")
        self.connect_button = self.button(connection, "Kết nối", self.toggle_connection)
        self.find_button = self.button(connection, "Tìm probe", self.find_probe)
        self.usb_button = self.button(connection, "Tìm IP qua USB", self.find_usb)
        self.wifi_button = self.button(connection, "Cài Wi-Fi", self.setup_wifi)
        self.mode_button = self.button(
            connection, "Đổi chế độ ESP", self.change_usb_mode
        )

        link = ttk.Frame(self)
        link.pack(fill="x", pady=(0, 6))
        steps = ttk.Frame(link)
        steps.pack(fill="x")
        self.link_indicators = [ttk.Label(steps) for _ in LINK_STEPS]
        for indicator in self.link_indicators:
            indicator.pack(side="left", padx=(0, 16))
        self.link_pulse = ttk.Progressbar(steps, length=90, mode="indeterminate")
        ttk.Label(steps, textvariable=self.core_text).pack(side="right")
        self.link_detail_label = ttk.Label(
            link, textvariable=self.link_detail, wraplength=760
        )
        self.link_detail_label.pack(anchor="w", pady=(3, 0), fill="x", expand=True)
        self.show_link("idle", self.link_detail.get())

        debug = ttk.Frame(self)
        debug.pack(fill="x", pady=(0, 6))
        self.halt_button = self.button(
            debug, "Dừng chip", lambda: self.run_control("halt")
        )
        self.resume_button = self.button(
            debug, "Chạy tiếp", lambda: self.run_control("resume")
        )
        self.program_button = self.button(debug, "Nạp ELF", self.program)
        self.verify_button = self.button(debug, "So khớp ELF", self.check_elf)
        self.button(debug, "Đồ thị / CSV", self.open_plot)
        self.rtt_button = self.button(debug, "Log RTT", self.open_rtt)
        self.save_preset_button = self.button(debug, "Lưu tuning", self.save_preset)
        self.apply_preset_button = self.button(debug, "Áp tuning", self.choose_preset)

        # Tabbed workspace: Tab 1 = Chip Debug, Tab 2 = Control & Robotics
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, pady=(2, 4))

        self.tab_debug = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(self.tab_debug, text=" 🐞 Debug chip ")
        self.setup_watch_ui(self.tab_debug)

        self.tab_robotics = RoboticsTab(self.notebook, app=self)
        self.notebook.add(self.tab_robotics, text=" 🤖 Điều khiển & Robot ")

        self.status_label = ttk.Label(self, textvariable=self.status, wraplength=760)
        self.status_label.pack(fill="x", pady=(2, 0))

        self.pack(fill="both", expand=True)

        self.load_profile()
        self.refresh_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._on_window_configure)
        atexit.register(self.stop_link)
        self.root.after_idle(self.poll)
        self.root.after(100, self.startup)

    @staticmethod
    def button(parent, label, command):
        button = ttk.Button(parent, text=label, command=command)
        button.pack(side="left", padx=(0, 6))
        return button

    def _on_transport_changed(self, _event=None):
        transport = self.transport.get()
        if transport == "Bluetooth":
            cur = self.ip.get().strip()
            if cur and "." in cur:
                self.wifi_ip = cur
            self.ip.set(self.ble_address or "")
            self.refresh_buttons()

            def check_usb_ble():
                probes = usb_probes()
                if probes:
                    try:
                        mode = get_usb_mode(probes[0])
                        if mode == "wifi":
                            return "Chế độ Bluetooth. ESP32 đang ở Wi-Fi. Bấm 'Đổi chế độ ESP' hoặc giữ nút BOOT 1.5s để đổi sang BLE."
                    except Exception:
                        pass
                return "Chế độ Bluetooth. Bấm 'Tìm probe' để quét BLE gần đây."

            self.executor.submit(check_usb_ble).add_done_callback(
                lambda f: self.root.after_idle(
                    lambda: self.connection_text.set(f.result()) if f.result() else None
                )
            )
        else:
            cur = self.ip.get().strip()
            if cur and (":" in cur or "-" in cur):
                self.ble_address = cur
            self.ip.set(self.wifi_ip or "")
            self.refresh_buttons()

            def check_usb_wifi():
                probes = usb_probes()
                if probes:
                    try:
                        mode = get_usb_mode(probes[0])
                        if mode == "ble":
                            return "Chế độ Wi-Fi. ESP32 đang ở Bluetooth. Bấm 'Đổi chế độ ESP' hoặc giữ nút BOOT 1.5s để đổi sang Wi-Fi."
                    except Exception:
                        pass
                return "Chế độ Wi-Fi. Nhập IP hoặc bấm 'Tìm probe'."

            self.executor.submit(check_usb_wifi).add_done_callback(
                lambda f: self.root.after_idle(
                    lambda: self.connection_text.set(f.result()) if f.result() else None
                )
            )

    def load_profile(self):
        settings = self.profiles.profiles[self.profile]
        self.wifi_ip = self.setting_text(settings, "ip")
        self.ble_address = self.setting_text(settings, "ble_address")
        transport = self.setting_text(settings, "transport")
        self.transport.set(transport if transport in TRANSPORTS else TRANSPORTS[0])
        if self.transport.get() == "Bluetooth":
            self.ip.set(self.ble_address)
        else:
            self.ip.set(self.wifi_ip)
        self.last_ip = self.ip.get()
        self.mac = self.setting_text(settings, "mac")
        target = self.setting_text(settings, "target")
        self.target.set(target if target in TARGETS else "STM32F4")
        self.presets = (
            settings.get("presets") if isinstance(settings.get("presets"), dict) else {}
        )
        self.clear_watch()
        self.available = {}
        self.combo["values"] = []
        self.variable.set("")
        self.elf_text.set("Chọn ELF của firmware đang chạy")
        self.elf_path = self.elf_version = self.verified = None
        path = self.setting_text(settings, "elf")
        if path and Path(path).is_file() and super().open_elf(path):
            self.elf_path = self.elf_version[0]
        elif path:
            self.elf_text.set(f"Mở lại ELF để dùng symbol · {path}")
        watched = settings.get("watch")
        self.restore_watch(watched if isinstance(watched, list) else ())
        self.sync_plot()

    @staticmethod
    def setting_text(settings, key):
        value = settings.get(key)
        return value if isinstance(value, str) else ""

    def restore_watch(self, watched):
        for item in watched:
            if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
                symbol = self.available.get(item[0])
                kind = item[1]
            elif isinstance(item, dict):
                kind = item.get("format")
                try:
                    symbol = manual_symbol(
                        item["address"], item["size"], item["kind"], item["name"]
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            else:
                continue
            if symbol is None:
                continue
            if symbol.is_group:
                row = self.add_symbol(symbol)
                if isinstance(kind, dict):
                    self.restore_group(row, kind)
            elif kind in (*symbol.formats, "hex"):
                self.add_symbol(symbol, kind)

    def restore_group(self, row, settings):
        expanded = settings.get("expanded")
        formats = settings.get("formats")
        for child in (row, *self.descendants(row)):
            path = self.paths[child]
            if child in self.groups and isinstance(expanded, list):
                self.rows.item(child, open=path in expanded)
            elif child in self.watch and isinstance(formats, dict) and path in formats:
                self.set_format(child, formats[path])

    def group_settings(self, row):
        expanded = []
        formats = {}
        for child in (row, *self.descendants(row)):
            path = self.paths[child]
            if child in self.groups and self.rows.item(child, "open"):
                expanded.append(path)
            elif child in self.watch:
                formats[path] = self.watch[child][1]
        return {"expanded": expanded, "formats": formats}

    def watch_settings(self):
        settings = []
        for row in self.rows.get_children():
            if row in self.groups:
                settings.append([self.paths[row], self.group_settings(row)])
            elif row in self.watch:
                symbol, kind = self.watch[row]
                if symbol.manual:
                    settings.append(
                        {
                            "name": symbol.name,
                            "address": symbol.address,
                            "size": symbol.size,
                            "kind": symbol.kind,
                            "format": kind,
                        }
                    )
                else:
                    settings.append([self.paths[row], kind])
        return settings

    def save_settings(self):
        if self.transport.get() == "Wi-Fi":
            cur = self.ip.get().strip()
            if cur and "." in cur:
                self.wifi_ip = cur
        else:
            cur = self.ip.get().strip()
            if cur and (":" in cur or "-" in cur):
                self.ble_address = cur
        settings = {
            "transport": self.transport.get(),
            "ip": self.wifi_ip or "",
            "mac": self.mac,
            "ble_address": self.ble_address or "",
            "target": self.target.get(),
            "elf": self.elf_path or "",
            "watch": self.watch_settings(),
            "presets": self.presets,
        }
        self.profiles.save(self.profile, settings)

    def change_profile(self, _event=None):
        if self.activity is not None or self.pending_write is not None:
            self.profile_name.set(self.profile)
            return
        if self.profile_name.get() == self.profile:
            return
        self.save_settings()
        self.profile = self.profile_name.get()
        self.profiles.set_active(self.profile)
        self.load_profile()
        self.refresh_buttons()
        self.startup()

    def save_profile_as(self):
        name = simpledialog.askstring(
            "Lưu profile mới", "Tên profile mới", parent=self.root
        )
        if not name or not name.strip():
            return
        name = name.strip()
        self.save_settings()
        self.profiles.duplicate(self.profile, name)
        self.profile = name
        self.profile_name.set(name)
        self.profile_box["values"] = list(self.profiles.profiles)
        self.save_settings()

    def choose_preset(self):
        if not self.presets or not self.can_use_target():
            return
        names = list(self.presets)
        dialog = tk.Toplevel(self.root)
        dialog.title("Áp dụng preset tuning")
        dialog.transient(self.root)
        dialog.grab_set()
        box = ttk.Combobox(dialog, values=names, state="readonly", width=32)
        box.set(names[0])
        box.pack(padx=12, pady=12)

        def apply_selected():
            name = box.get()
            dialog.destroy()
            self.apply_preset(name)

        ttk.Button(dialog, text="Áp dụng", command=apply_selected).pack(pady=(0, 12))

    def apply_preset(self, name):
        preset = self.presets.get(name)
        if not preset or self.activity is not None or not self.can_use_target():
            return
        self.activity = "preset"
        self.connection_text.set(f"Đang áp dụng preset {name}...")
        self.refresh_buttons()
        session = self.write_session()

        def write_values():
            written = 0
            with OpenOcd(self.port) as ocd:
                for item in preset:
                    if not self.same_session(session) or self.closing:
                        break
                    symbol = manual_symbol(
                        item["address"], item["size"], item["kind"], item["name"]
                    )
                    ocd.write(symbol, item["raw"])
                    written += 1
            return written

        self.executor.submit(write_values).add_done_callback(
            lambda future: self.root.after_idle(self._apply_preset_done, name, future)
        )

    def _apply_preset_done(self, name, future):
        self.activity = None
        try:
            count = future.result()
            self.connection_text.set(f"Đã ghi {count} giá trị cho preset {name}.")
        except Exception as error:
            self.connection_text.set(f"Lỗi áp dụng preset · {error}")
        self.refresh_buttons()

    def save_preset(self):
        rows = list(self.watch)
        if not rows:
            messagebox.showinfo(
                "Lưu tuning", "Chưa có biến nào để lưu.", parent=self.root
            )
            return
        options = []
        for row in rows:
            if row in self.raw_values:
                symbol, kind = self.watch[row]
                options.append((self.paths[row], symbol, kind, self.raw_values[row]))
        if not options:
            messagebox.showinfo(
                "Lưu tuning", "Chưa có giá trị nào được đọc từ chip.", parent=self.root
            )
            return
        dialog = PresetDialog(self.root, options)
        if dialog.result is not None:
            name, values = dialog.result
            self.presets[name] = values
            self.save_settings()
            self.connection_text.set(f"Đã lưu preset {name}.")

    def open_elf(self, path):
        if self.activity is not None or self.closing:
            return
        manual_watch = [
            (symbol, kind) for symbol, kind in self.watch.values() if symbol.manual
        ]
        if super().open_elf(path):
            self.elf_path = str(Path(path).resolve())
            self.verified = None
            for symbol, kind in manual_watch:
                self.add_symbol(symbol, kind)
            self.save_settings()
            self.sync_plot()
            self.refresh_buttons()

    def read_symbols(self, path):
        resolved = str(Path(path).resolve())
        version = self.elf_signature(resolved)
        symbols = super().read_symbols(resolved)
        if self.elf_signature(resolved) != version:
            raise RuntimeError(
                "ELF vừa thay đổi trong lúc mở. Mở lại file sau khi build xong."
            )
        self.elf_version = version
        return symbols

    @staticmethod
    def elf_signature(path):
        stat = Path(path).stat()
        return (path, stat.st_size, stat.st_mtime_ns)

    def startup(self):
        if self.closing or self.keep_link or self.activity is not None:
            return
        if self.transport.get() == "Wi-Fi" and self.ip.get().strip():
            self.toggle_connection()
        elif self.transport.get() == "Bluetooth" and (
            self.ble_address or self.ip.get().strip()
        ):
            self.toggle_connection()

    def refresh_buttons(self):
        transport = self.transport.get()
        busy = self.activity is not None
        connected = bool(self.link_status)
        self.connect_button.configure(
            text="Ngắt" if connected or busy else "Kết nối",
            state="normal" if not self.closing else "disabled",
        )
        self.find_button.configure(
            state="normal" if not busy and not self.closing else "disabled"
        )
        self.usb_button.configure(
            state="normal" if not busy and not self.closing else "disabled"
        )
        self.wifi_button.configure(
            state="normal" if not busy and not self.closing else "disabled"
        )
        self.mode_button.configure(
            state="normal" if not busy and not self.closing else "disabled"
        )
        self.ip_entry.configure(
            state="normal" if not busy and not self.closing else "disabled"
        )
        can_target = self.can_use_target()
        self.halt_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )
        self.resume_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )
        self.program_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )
        self.verify_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )
        self.save_preset_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )
        self.apply_preset_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )
        self.rtt_button.configure(
            state="normal" if can_target and not busy else "disabled"
        )

    def show_link(self, state, detail=""):
        if not self.winfo_exists():
            return
        labels = {
            "idle": "Chưa kết nối",
            "working": "Đang kết nối...",
            "ready": "Đã kết nối",
            "error": "Lỗi kết nối.",
        }
        if state == "working":
            self.link_pulse.pack(side="left", padx=8)
            self.link_pulse.start(10)
        else:
            self.link_pulse.stop()
            self.link_pulse.pack_forget()

        for idx, (code, label) in enumerate(LINK_STEPS):
            lbl = self.link_indicators[idx]
            if state == "ready":
                lbl.configure(text=f"✓ {label}", foreground="#2E7D32")
            elif state == "working":
                if idx < self.link_step:
                    lbl.configure(text=f"✓ {label}", foreground="#2E7D32")
                elif idx == self.link_step:
                    lbl.configure(text=f"● {label}", foreground="#1565C0")
                else:
                    lbl.configure(text=f"○ {label}", foreground="#757575")
            elif state == "error" and idx == self.link_step:
                lbl.configure(text=f"! {label}", foreground="#C62828")
            else:
                lbl.configure(text=f"○ {label}", foreground="#757575")

        if detail:
            self.link_detail.set(detail)

    def set_core_state(self, state):
        self.core_state = state
        self.core_text.set(f"STM32 · {CORE_LABELS.get(state, state)}")

    def probe_alive(self):
        with OpenOcd(self.port) as ocd:
            return ocd._call('set _res ""; catch {[target current] curstate} _res; set _res')

    def can_use_target(self):
        return bool(self.link_status) and self.activity is None

    def write_session(self):
        return self.link_status

    def toggle_connection(self):
        if self.link_status or self.activity is not None:
            self.stop_link()
        else:
            self.begin_connect()

    def begin_connect(self):
        if self.activity is not None or self.closing:
            return
        transport = self.transport.get()
        target = self.target.get()
        ip = self.ip.get().strip()

        if transport == "Wi-Fi":
            if not ip:
                messagebox.showerror(
                    "Thiếu địa chỉ IP",
                    "Nhập IP của ESP32 hoặc bấm 'Tìm probe'.",
                    parent=self.root,
                )
                return
            self.activity = "connect"
            self.link_status = ""
            self.link_step = 0
            self.show_link("working", f"Đang kết nối tới probe Wi-Fi {ip}...")
            self.refresh_buttons()
            self.executor.submit(self._worker_connect_wifi, ip, target)
        else:
            self.activity = "connect"
            self.link_status = ""
            self.link_step = 0
            self.show_link("working", "Đang kết nối qua Bluetooth...")
            self.refresh_buttons()
            self.executor.submit(self._worker_connect_ble, target)

    def _worker_connect_wifi(self, ip, target):
        try:
            self.link_step = 1
            self.server.start_wifi(ip, target, self.port)
            self.link_step = 2
            self.server.wait_target(self.port)
            self.link_status = f"wifi://{ip}"
            self.root.after_idle(self._connect_succeeded)
        except Exception as error:
            self.root.after_idle(self._connect_failed, error)

    def _worker_connect_ble(self, target):
        try:
            self.link_step = 0
            if self.bridge is None:
                addr = self.ble_address or self.ip.get().strip() or None
                if addr and "." in addr and ":" not in addr:
                    addr = None
                self.bridge = BleBridge(address=addr)
                bridge_port = self.bridge.start()
            else:
                bridge_port = self.bridge.port
            if self.bridge.probe_address:
                self.ble_address = self.bridge.probe_address
                self.root.after_idle(lambda: self.ip.set(self.bridge.probe_address))
            self.link_step = 1
            self.server.start_ble(bridge_port, target, self.port)
            self.link_step = 2
            self.server.wait_target(self.port)
            self.link_status = "ble://active"
            self.root.after_idle(self._connect_succeeded)
        except Exception as error:
            self.root.after_idle(self._connect_failed, error)

    def _connect_succeeded(self):
        self.activity = None
        self.show_link("ready", f"Đã kết nối SWD tới {self.target.get()}.")
        self.set_core_state("running")
        self.save_settings()
        self.refresh_buttons()

    def _connect_failed(self, error):
        self.activity = None
        self.server.stop()
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        self.show_link("error", str(error))
        self.set_core_state("unavailable")
        self.refresh_buttons()

    def stop_link(self):
        self.link_status = ""
        self.server.stop()
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        try:
            if self.winfo_exists():
                self.show_link("idle", "Đã ngắt kết nối.")
                self.set_core_state("Chưa kết nối")
                self.refresh_buttons()
        except Exception:
            pass

    def run_control(self, cmd):
        if not self.can_use_target():
            return

        def worker():
            with OpenOcd(self.port) as ocd:
                ocd._call(cmd)

        self.executor.submit(worker)

    def program(self):
        if not self.can_use_target() or not self.elf_path:
            messagebox.showinfo(
                "Nạp ELF", "Chọn file ELF trước khi nạp.", parent=self.root
            )
            return
        self.activity = "program"
        self.connection_text.set("Đang nạp firmware...")
        self.refresh_buttons()

        def worker():
            with OpenOcd(self.port) as ocd:
                ocd._call(f"program {{{self.elf_path}}} verify reset")

        self.executor.submit(worker).add_done_callback(
            lambda future: self.root.after_idle(self._program_done, future)
        )

    def _program_done(self, future):
        self.activity = None
        try:
            future.result()
            self.connection_text.set("Nạp firmware thành công.")
        except Exception as error:
            self.connection_text.set(f"Lỗi nạp firmware · {error}")
        self.refresh_buttons()

    def check_elf(self):
        if not self.can_use_target() or not self.elf_path:
            return
        self.activity = "verify"
        self.connection_text.set("Đang so khớp bộ nhớ...")
        self.refresh_buttons()

        def worker():
            with OpenOcd(self.port) as ocd:
                return ocd._call(f"verify_image {{{self.elf_path}}}")

        self.executor.submit(worker).add_done_callback(
            lambda future: self.root.after_idle(self._verify_done, future)
        )

    def _verify_done(self, future):
        self.activity = None
        try:
            res = future.result()
            self.connection_text.set(f"So khớp ELF: {res}")
        except Exception as error:
            self.connection_text.set(f"Lỗi so khớp · {error}")
        self.refresh_buttons()

    def open_plot(self):
        if self.plot is not None and self.plot.winfo_exists():
            self.plot.lift()
            return
        self.plot = PlotWindow(self.root)
        self.sync_plot()

    def sync_plot(self):
        if self.plot is not None and self.plot.winfo_exists():
            self.plot.sync_series(
                {
                    row: f"{self.paths[row]} (0x{symbol.address:08X})"
                    for row, (symbol, _) in self.watch.items()
                }
            )

    def open_rtt(self):
        if self.rtt_window is not None and self.rtt_window.winfo_exists():
            self.rtt_window.lift()
            return
        if not self.can_use_target():
            messagebox.showinfo(
                "Log RTT", "Cần kết nối tới target trước.", parent=self.root
            )
            return
        address = simpledialog.askstring(
            "Địa chỉ RTT",
            "Nhập địa chỉ RTT Control Block (hex, e.g. 0x20000000) hoặc để trống để tự tìm:",
            parent=self.root,
        )
        if address is None:
            return
        addr_int = None
        if address.strip():
            try:
                addr_int = int(address.strip(), 16)
            except ValueError:
                messagebox.showerror(
                    "Lỗi địa chỉ", "Địa chỉ hex không hợp lệ.", parent=self.root
                )
                return
        if addr_int is None and self.elf_path:
            addr_int = rtt_address(self.elf_path)
        if addr_int is None:
            messagebox.showwarning(
                "Tìm RTT",
                "Không tìm thấy symbol _SEGGER_RTT trong file ELF. Vui lòng nhập địa chỉ cụ thể.",
                parent=self.root,
            )
            return
        self.rtt_window = RttWindow(self.root, self.port, addr_int)

    def find_probe(self):
        if self.transport.get() == "Bluetooth":
            self.connection_text.set("Đang quét probe BLE gần đây...")

            def worker():
                return discover_ble_probes()

            self.executor.submit(worker).add_done_callback(
                lambda f: self.root.after_idle(self._ble_probes_found, f)
            )
        else:
            self.connection_text.set("Đang quét probe Wi-Fi trong mạng LAN...")

            def worker():
                return discover_wifi()

            self.executor.submit(worker).add_done_callback(
                lambda f: self.root.after_idle(self._wifi_probes_found, f)
            )

    def _ble_probes_found(self, future):
        try:
            probes = future.result()
            if not probes:
                hint = ""
                probes_usb = usb_probes()
                if probes_usb:
                    try:
                        mode = get_usb_mode(probes_usb[0])
                        if mode == "wifi":
                            hint = " (ESP32 đang ở chế độ Wi-Fi. Bấm 'Đổi chế độ ESP' hoặc giữ nút BOOT 1.5s trên chip để sang Bluetooth)."
                    except Exception:
                        pass
                self.connection_text.set(
                    "Không tìm thấy probe BLE nào gần đây."
                    + (
                        hint
                        or " Hãy đảm bảo ESP32 đang ở chế độ Bluetooth (nháy LED xanh dương)."
                    )
                )
                return
            self.ble_address = probes[0].address
            self.ip.set(probes[0].address)
            self.connection_text.set(
                f"Đã tìm thấy probe BLE: {probes[0].name or probes[0].address} ({probes[0].address})"
            )
        except Exception as error:
            self.connection_text.set(f"Lỗi quét BLE · {error}")

    def _wifi_probes_found(self, future):
        try:
            probes = future.result()
            if not probes:
                hint = ""
                probes_usb = usb_probes()
                if probes_usb:
                    try:
                        mode = get_usb_mode(probes_usb[0])
                        if mode == "ble":
                            hint = " (ESP32 đang ở chế độ Bluetooth. Bấm 'Đổi chế độ ESP' hoặc giữ nút BOOT 1.5s trên chip để sang Wi-Fi)."
                    except Exception:
                        pass
                self.connection_text.set(
                    "Không tìm thấy probe Wi-Fi nào trên LAN."
                    + (hint or " Hãy kiểm tra router hoặc cấu hình Wi-Fi.")
                )
                return
            ip = probes[0][1]
            self.wifi_ip = ip
            self.ip.set(ip)
            self.connection_text.set(f"Đã tìm thấy probe Wi-Fi: {ip}")
        except Exception as error:
            self.connection_text.set(f"Lỗi quét Wi-Fi · {error}")

    def find_usb(self):
        self.connection_text.set("Đang tìm ESP32 cắm qua USB...")

        def worker():
            probes = usb_probes()
            if not probes:
                raise RuntimeError("Không tìm thấy probe ESP32 cắm qua USB.")
            port = probes[0]
            mode = get_usb_mode(port)
            ip = None
            if mode == "wifi":
                try:
                    ip = usb_ip(port)
                except Exception:
                    pass
            return port, mode, ip

        self.executor.submit(worker).add_done_callback(
            lambda f: self.root.after_idle(self._usb_info_found, f)
        )

    def _usb_info_found(self, future):
        try:
            port, mode, ip = future.result()
            mode_str = (
                "Wi-Fi"
                if mode == "wifi"
                else ("Bluetooth (BLE)" if mode == "ble" else str(mode))
            )
            if mode == "wifi" and ip:
                self.wifi_ip = ip
                if self.transport.get() == "Wi-Fi":
                    self.ip.set(ip)
                self.connection_text.set(
                    f"ESP32 cắm {port}: Chế độ {mode_str}, IP = {ip}"
                )
            elif mode == "wifi":
                self.connection_text.set(
                    f"ESP32 cắm {port}: Chế độ {mode_str} (chưa có IP, bấm 'Cài Wi-Fi')"
                )
            else:
                self.connection_text.set(f"ESP32 cắm {port}: Chế độ {mode_str}")
        except Exception as error:
            self.connection_text.set(f"Lỗi đọc USB · {error}")

    def setup_wifi(self):
        from tkinter import simpledialog

        ssid = simpledialog.askstring(
            "Cài đặt Wi-Fi", "Nhập tên mạng (SSID):", parent=self.root
        )
        if not ssid:
            return
        password = simpledialog.askstring(
            "Cài đặt Wi-Fi", "Nhập mật khẩu Wi-Fi:", parent=self.root, show="*"
        )
        if password is None:
            return

        is_ble = self.transport.get() == "Bluetooth"
        if is_ble:
            self.connection_text.set("Đang gửi cấu hình Wi-Fi qua Bluetooth...")

            def worker():
                target_addr = self.ble_address or self.ip.get().strip()
                if not target_addr:
                    probes = discover_ble_probes(timeout=2.5)
                    if probes:
                        target_addr = probes[0].address
                if not target_addr:
                    raise RuntimeError(
                        "Không tìm thấy địa chỉ BLE của probe. Hãy nhấn 'Tìm probe' trước."
                    )
                return configure_wifi_ble(target_addr, ssid, password)
        else:
            self.connection_text.set("Đang gửi cấu hình Wi-Fi qua USB...")

            def worker():
                probes = usb_probes()
                if not probes:
                    raise RuntimeError("Không tìm thấy probe ESP32 cắm qua USB.")
                return configure_wifi(probes[0], ssid, password)

        self.executor.submit(worker).add_done_callback(
            lambda f: self.root.after_idle(self._wifi_configured, f)
        )

    def _wifi_configured(self, future):
        try:
            res = future.result()
            msg = (
                f"Đã lưu cấu hình Wi-Fi ({res})"
                if res
                else "Đã lưu cấu hình Wi-Fi vào ESP32."
            )
            self.connection_text.set(msg)
        except Exception as error:
            self.connection_text.set(f"Lỗi cấu hình Wi-Fi · {error}")

    def change_usb_mode(self):
        modes = ["Wi-Fi", "Bluetooth"]
        dialog = tk.Toplevel(self.root)
        dialog.title("Chọn chế độ ESP32")
        dialog.transient(self.root)
        dialog.grab_set()
        box = ttk.Combobox(dialog, values=modes, state="readonly", width=16)
        curr = "Bluetooth" if self.transport.get() == "Bluetooth" else "Wi-Fi"
        box.set(curr)
        box.pack(padx=16, pady=12)

        def apply():
            target_mode = "wifi" if box.get() == "Wi-Fi" else "ble"
            chosen_label = box.get()
            dialog.destroy()
            self.connection_text.set(f"Đang đổi chế độ ESP32 sang {chosen_label}...")

            def worker():
                probes = usb_probes()
                if not probes:
                    raise RuntimeError("Không tìm thấy probe ESP32 cắm qua USB.")
                set_usb_mode(probes[0], target_mode)
                return chosen_label

            self.executor.submit(worker).add_done_callback(
                lambda f: self.root.after_idle(self._mode_changed, f)
            )

        ttk.Button(dialog, text="Đổi chế độ", command=apply).pack(pady=(0, 12))

    def _mode_changed(self, future):
        try:
            chosen = future.result()
            self.transport.set(chosen)
            self._on_transport_changed()
            self.connection_text.set(
                f"Đã đổi chế độ ESP32 sang {chosen}. Đang khởi động lại..."
            )
        except Exception as error:
            self.connection_text.set(f"Lỗi đổi chế độ · {error}")

    def _on_window_configure(self, event):
        if event.widget == self.root:
            w = event.width
            wrap = max(300, w - 60)
            if hasattr(self, "link_detail_label"):
                self.link_detail_label.configure(wraplength=wrap)
            if hasattr(self, "status_label"):
                self.status_label.configure(wraplength=wrap)
            if hasattr(self, "connection_label"):
                self.connection_label.configure(wraplength=max(200, w - 380))

    def close(self):
        if not self.closing:
            self.save_settings()
            self.closing = True
            self.verify_cancel.set()
            try:
                atexit.unregister(self.stop_link)
            except Exception:
                pass
            self.stop_link()
            super().close()


def main():
    if "LOCALAPPDATA" in os.environ:
        folder = Path(os.environ["LOCALAPPDATA"]) / "STM32 Wireless Debug"
    elif (Path.home() / "STM32 Wireless Debug").is_dir():
        folder = Path.home() / "STM32 Wireless Debug"
    else:
        folder = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "STM32 Wireless Debug"
        )
    folder.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.title("STM32 Wireless Debug 1.3")
    root.geometry("1060x660")
    root.minsize(860, 480)
    DesktopApp(root, folder)
    root.mainloop()


if __name__ == "__main__":
    main()
