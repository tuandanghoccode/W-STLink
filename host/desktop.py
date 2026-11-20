"""Standalone Windows app for the wireless STM32 probe."""

from ipaddress import IPv4Address
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event
from time import monotonic
import os
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from ble_bridge import BleBridge, discover_ble_probes
from live_memory import OpenOcd, display_value, manual_symbol, numeric_value
from live_watch import LiveWatch
from plot_window import PlotWindow
from probe_server import ProbeServer, TARGETS, configure_wifi, discover_wifi, set_usb_mode, usb_ip, usb_probes
from rtt_window import RttWindow
from target_tools import RttCapture, rtt_address, verify_elf
from watch_profiles import ProfileStore

TRANSPORTS = ("Wi-Fi", "Bluetooth")
CHECK_INTERVAL = 4
RETRY_INTERVAL = 6
LINK_STEPS = (("probe", "Chuẩn bị ESP32"), ("openocd", "Mở OpenOCD"), ("swd", "Kiểm tra SWD"))
CORE_LABELS = {"running": "Đang chạy", "debug-running": "Đang chạy debug", "halted": "Đã dừng",
               "reset": "Đang reset", "unavailable": "Chưa truy cập được", "unknown": "Chưa rõ trạng thái"}


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
        self.variables = tk.Listbox(container, selectmode="extended", height=min(12, len(self.options)),
                                    width=55, exportselection=False)
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
            messagebox.showerror("Thiếu thông tin", "Nhập tên và chọn ít nhất một biến.", parent=self)
            return False
        self.result = (name, [
            {"name": symbol.label, "manual": symbol.manual, "address": symbol.address,
             "size": symbol.size, "kind": symbol.kind, "raw": raw}
            for _, symbol, _, raw in (self.options[index] for index in selected)
        ])
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

        super().__init__(root, None, 6666)
        self.connection_text = tk.StringVar(value="Chọn cách kết nối ESP32.")
        self.profile_name = tk.StringVar(value=self.profile)
        self.transport = tk.StringVar(value=TRANSPORTS[0])
        self.ip = tk.StringVar()
        self.target = tk.StringVar(value="STM32F4")
        self.core_text = tk.StringVar(value="STM32 · Chưa kết nối")
        self.link_detail = tk.StringVar(value="Chọn Wi-Fi hoặc Bluetooth rồi kết nối.")

        profiles = ttk.Frame(self)
        profiles.pack(fill="x", pady=(0, 8), before=self.winfo_children()[0])
        ttk.Label(profiles, text="Profile").pack(side="left")
        self.profile_box = ttk.Combobox(profiles, textvariable=self.profile_name, values=list(self.profiles.profiles),
                                        width=22, state="readonly")
        self.profile_box.pack(side="left", padx=6)
        self.profile_box.bind("<<ComboboxSelected>>", self.change_profile)
        self.profile_button = self.button(profiles, "Lưu profile mới", self.save_profile_as)
        ttk.Label(profiles, textvariable=self.connection_text, wraplength=580).pack(side="left", padx=8)

        connection = ttk.Frame(self)
        connection.pack(fill="x", pady=(0, 8), after=profiles)
        self.transport_box = ttk.Combobox(connection, textvariable=self.transport, values=TRANSPORTS,
                                          width=10, state="readonly")
        self.transport_box.pack(side="left")
        self.transport_box.bind("<<ComboboxSelected>>", lambda _: self.refresh_buttons())
        self.ip_entry = ttk.Entry(connection, textvariable=self.ip, width=16)
        self.ip_entry.pack(side="left", padx=6)
        self.ip_entry.bind("<Return>", lambda _: self.toggle_connection())
        self.target_box = ttk.Combobox(connection, textvariable=self.target, values=list(TARGETS),
                                       width=10, state="readonly")
        self.target_box.pack(side="left")
        self.connect_button = self.button(connection, "Kết nối", self.toggle_connection)
        self.find_button = self.button(connection, "Tìm probe", self.find_probe)
        self.usb_button = self.button(connection, "Tìm IP qua USB", self.find_usb)
        self.wifi_button = self.button(connection, "Cài Wi-Fi", self.setup_wifi)
        self.mode_button = self.button(connection, "Đổi chế độ ESP", self.change_usb_mode)

        link = ttk.Frame(self)
        link.pack(fill="x", pady=(0, 8), after=connection)
        steps = ttk.Frame(link)
        steps.pack(fill="x")
        self.link_indicators = [ttk.Label(steps) for _ in LINK_STEPS]
        for indicator in self.link_indicators:
            indicator.pack(side="left", padx=(0, 16))
        self.link_pulse = ttk.Progressbar(steps, length=90, mode="indeterminate")
        ttk.Label(steps, textvariable=self.core_text).pack(side="right")
        ttk.Label(link, textvariable=self.link_detail, wraplength=760).pack(anchor="w", pady=(3, 0))
        self.show_link("idle", self.link_detail.get())

        debug = ttk.Frame(self)
        debug.pack(fill="x", pady=(0, 8), after=link)
        self.halt_button = self.button(debug, "Dừng chip", lambda: self.run_control("halt"))
        self.resume_button = self.button(debug, "Chạy tiếp", lambda: self.run_control("resume"))
        self.program_button = self.button(debug, "Nạp ELF", self.program)
        self.verify_button = self.button(debug, "So khớp ELF", self.check_elf)
        self.button(debug, "Đồ thị / CSV", self.open_plot)
        self.rtt_button = self.button(debug, "Log RTT", self.open_rtt)
        self.save_preset_button = self.button(debug, "Lưu tuning", self.save_preset)
        self.apply_preset_button = self.button(debug, "Áp tuning", self.choose_preset)

        self.load_profile()
        self.refresh_buttons()
        self.root.after(100, self.startup)

    @staticmethod
    def button(parent, label, command):
        button = ttk.Button(parent, text=label, command=command)
        button.pack(side="left", padx=(0, 6))
        return button

    def load_profile(self):
        settings = self.profiles.profiles[self.profile]
        self.ip.set(self.setting_text(settings, "ip"))
        self.last_ip = self.ip.get()
        self.mac = self.setting_text(settings, "mac")
        self.ble_address = self.setting_text(settings, "ble_address")
        transport = self.setting_text(settings, "transport")
        self.transport.set(transport if transport in TRANSPORTS else TRANSPORTS[0])
        target = self.setting_text(settings, "target")
        self.target.set(target if target in TARGETS else "STM32F4")
        self.presets = settings.get("presets") if isinstance(settings.get("presets"), dict) else {}
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
                    symbol = manual_symbol(item["address"], item["size"], item["kind"], item["name"])
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
            if child in self.groups:
                if self.rows.item(child, "open"):
                    expanded.append(self.paths[child])
            else:
                symbol, kind = self.watch[child]
                if kind != symbol.kind:
                    formats[self.paths[child]] = kind
        return {"expanded": expanded, "formats": formats}

    def save_settings(self):
        watched = []
        for row in self.rows.get_children():
            symbol = self.root_symbol(row)
            if symbol.is_group:
                watched.append([symbol.label, self.group_settings(row)])
            elif symbol.manual:
                kind = self.watch[row][1]
                watched.append({"name": symbol.name, "address": symbol.address, "size": symbol.size,
                                "kind": symbol.kind, "format": kind})
            else:
                watched.append([symbol.label, self.watch[row][1]])
        try:
            self.profiles.save(self.profile, {
                "ip": self.ip.get().strip(), "mac": self.mac, "transport": self.transport.get(),
                "ble_address": self.ble_address, "target": self.target.get(),
                "elf": self.elf_path or "", "watch": watched, "presets": self.presets,
            })
            return True
        except OSError as error:
            self.connection_text.set(f"Lỗi lưu profile · {error}")
            return False

    def save_profile_as(self):
        name = simpledialog.askstring("Profile mới", "Tên profile", parent=self.root)
        if name is None or not (name := name.strip()):
            return
        if name in self.profiles.profiles:
            messagebox.showerror("Profile đã tồn tại", name, parent=self.root)
            return
        previous = self.profile
        self.profile = name
        if self.save_settings():
            self.presets = dict(self.presets)
            self.profile_box["values"] = list(self.profiles.profiles)
            self.profile_name.set(name)
            self.connection_text.set(f"Đã lưu profile {name}.")
        else:
            self.profile = previous

    def change_profile(self, _event):
        name = self.profile_name.get()
        if name == self.profile:
            return
        if not self.save_settings():
            self.profile_name.set(self.profile)
            return
        self.keep_link = False
        self.close_rtt_window()
        self.run_task("Đang đổi profile...", self.stop_link, result=lambda _: self.use_profile(name))

    def use_profile(self, name):
        if self.plot is not None and self.plot.winfo_exists():
            self.plot.destroy()
        self.clear_connection()
        self.profile = name
        self.load_profile()
        self.connection_text.set(f"Đã mở profile {name}.")
        self.save_settings()
        self.startup()

    def open_elf(self, path):
        if self.activity is not None or self.closing:
            return
        manual_watch = [(symbol, kind) for symbol, kind in self.watch.values() if symbol.manual]
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
            raise RuntimeError("ELF vừa thay đổi trong lúc mở. Mở lại file sau khi build xong.")
        self.elf_version = version
        return symbols

    @staticmethod
    def elf_signature(path):
        stat = Path(path).stat()
        return (path, stat.st_size, stat.st_mtime_ns)

    def startup(self):
        if self.closing or self.keep_link or self.activity is not None:
            return
        if self.transport.get() == "Bluetooth":
            if self.ble_address:
                self.begin_connect()
        elif self.ip.get().strip() or self.mac:
            self.begin_connect()


    def can_use_target(self):
        return (bool(self.link_status) and self.server.running and self.activity is None and not self.closing
                and (self.bridge is None or self.bridge.is_running))

    def write_session(self):
        return self.server.process

    def same_session(self, session):
        if super().same_session(session):
            return True
        self.connection_text.set("Kết nối đã đổi. Kiểm tra probe rồi thực hiện lại thao tác.")
        return False

    def refresh_buttons(self):
        idle = self.activity is None and self.pending_write is None and not self.closing
        ready = idle and self.can_use_target()
        editable = idle and not self.keep_link
        wifi = self.transport.get() == "Wi-Fi"
        self.ip_entry.configure(state="normal" if editable and wifi else "disabled")
        for box in (self.transport_box, self.target_box):
            box.configure(state="readonly" if editable else "disabled")
        self.profile_box.configure(state="readonly" if idle else "disabled")
        self.profile_button.configure(state="normal" if idle else "disabled")
        self.connect_button.configure(text="Ngắt" if self.keep_link else "Kết nối",
                                      state="normal" if idle else "disabled")
        for button in (self.usb_button, self.wifi_button, self.find_button, self.mode_button):
            button.configure(state="normal" if editable else "disabled")
        for button in (self.usb_button, self.wifi_button):
            if not wifi:
                button.configure(state="disabled")
        self.halt_button.configure(state="normal" if ready and self.core_state != "halted" else "disabled")
        running = self.core_state in ("running", "debug-running")
        self.resume_button.configure(state="normal" if ready and not running else "disabled")
        self.rtt_button.configure(state="normal" if ready else "disabled")
        for button in (self.program_button, self.verify_button):
            button.configure(state="normal" if ready and self.elf_path else "disabled")
        snapshot_ready = ready and any(self.watch[row][0].writable for row in self.raw_values)
        self.save_preset_button.configure(state="normal" if snapshot_ready else "disabled")
        self.apply_preset_button.configure(state="normal" if ready and self.presets else "disabled")
        self.update_selection()

    def run_task(self, status, function, *args, result=None, failed=None):
        if self.activity is None and not self.closing and self.pending_write is None:
            self.connection_text.set(status)
            self.activity = (self.executor.submit(function, *args), result, failed)
            self.refresh_buttons()

    def toggle_connection(self):
        if self.activity is not None or self.pending_write is not None or self.closing:
            return
        if self.keep_link:
            self.keep_link = False
            self.health = None
            self.close_rtt_window()
            self.run_task("Đang ngắt kết nối...", self.stop_link, result=lambda _: self.clear_connection())
        else:
            self.begin_connect()

    def begin_connect(self):
        if self.activity is not None or self.pending_write is not None or self.closing:
            return
        transport, ip = self.transport.get(), self.ip.get().strip()
        if transport == "Wi-Fi":
            if ip != self.last_ip:
                self.mac = ""
                self.last_ip = ip
            if not (ip or self.mac):
                self.find_probe()
                return
            if ip:
                try:
                    IPv4Address(ip)
                except ValueError:
                    self.connection_text.set("Nhập địa chỉ IPv4 hợp lệ của probe.")
                    return
        self.keep_link = True
        self.link_status = ""
        self.health = None
        self.link_events = SimpleQueue()
        self.set_core_state(None)
        self.show_link("active", f"Đang chuẩn bị kết nối qua {transport}.", "probe")
        self.close_rtt_window()
        self.reset_values()
        self.run_task("Đang kết nối probe và STM32...", self.start_link,
                      transport, ip, self.target.get(), self.mac, self.ble_address, self.link_events,
                      result=self.connected, failed=self.connection_failed)

    def start_link(self, transport, ip, target, mac, ble_address, events):
        def report(stage, detail):
            events.put((stage, detail))

        self.stop_link()
        try:
            if transport == "Bluetooth":
                self.bridge = BleBridge(ble_address or None)
                port = self.bridge.start(progress=report)
                status = self.server.start("127.0.0.1", target, port, progress=report)
                return status, ip, mac, self.bridge.probe_address
            if mac:
                report("probe", "Đang tìm địa chỉ Wi-Fi của ESP32 đã lưu.")
                ip = dict(discover_wifi(address=ip)).get(mac, "")
            if not ip:
                raise RuntimeError("Chưa tìm thấy ESP32 trong mạng Wi-Fi.")
            report("probe", f"Wi-Fi · ESP32 tại {ip}.")
            return self.server.start(ip, target, progress=report), ip, mac, ble_address
        except Exception:
            self.stop_link()
            raise

    def stop_link(self):
        self.verified = None
        try:
            if self.rtt is not None:
                self.rtt.stop()
        except (OSError, RuntimeError):
            pass
        finally:
            self.rtt = None
            try:
                self.server.stop()
            finally:
                if self.bridge is not None:
                    bridge, self.bridge = self.bridge, None
                    bridge.stop()

    def connected(self, info):
        self.link_status, ip, self.mac, self.ble_address = info
        self.ip.set(ip)
        self.last_ip = ip
        self.next_check = monotonic()
        self.show_link("ready", f"{self.transport.get()} · {self.link_status}")
        self.connection_text.set("Đã kết nối.")
        self.save_settings()

    def connection_failed(self, error):
        self.link_status = ""
        self.set_core_state(None)
        self.next_retry = monotonic() + RETRY_INTERVAL
        hint = " · Sẽ thử nối lại" if self.keep_link else ""
        self.show_link("error", f"{error}{hint}")
        self.connection_text.set("Lỗi kết nối.")

    def show_link(self, mode, detail, stage=None):
        if stage is not None:
            self.link_step = next(index for index, (key, _) in enumerate(LINK_STEPS) if key == stage)
        elif mode == "idle":
            self.link_step = -1
        for index, ((_, name), indicator) in enumerate(zip(LINK_STEPS, self.link_indicators)):
            if mode == "ready" or index < self.link_step:
                prefix, color = "✓", "#15703e"
            elif index == self.link_step and mode in ("active", "error"):
                prefix, color = ("●", "#1658a4") if mode == "active" else ("!", "#a12b26")
            else:
                prefix, color = "○", "#58636f"
            indicator.configure(text=f"{prefix} {name}", foreground=color)
        if mode == "active" and not self.link_pulse.winfo_manager():
            self.link_pulse.pack(side="left", padx=(0, 10))
            self.link_pulse.start(18)
        elif mode != "active" and self.link_pulse.winfo_manager():
            self.link_pulse.stop()
            self.link_pulse.pack_forget()
        self.link_detail.set(detail)

    def update_link_progress(self):
        while True:
            try:
                stage, detail = self.link_events.get_nowait()
            except Empty:
                return
            self.show_link("active", detail, stage)

    def set_core_state(self, state):
        self.core_state = state
        label = "Chưa kết nối" if state is None else CORE_LABELS.get(state, CORE_LABELS["unknown"])
        self.core_text.set(f"STM32 · {label}")

    def reset_values(self):
        self.pending_read = None
        self.raw_values.clear()
        for row in self.watch:
            self.rows.set(row, "value", "—")

    def clear_connection(self):
        self.health = None
        self.link_status = ""
        self.set_core_state(None)
        self.show_link("idle", "Đã ngắt kết nối ESP32.")
        self.reset_values()
        self.connection_text.set("Đã ngắt kết nối.")

    def probe_alive(self):
        if not self.server.running:
            raise RuntimeError("OpenOCD đã dừng.")
        if self.bridge is not None and not self.bridge.is_running:
            raise RuntimeError(self.bridge.last_error or "Bluetooth đã ngắt.")
        return self.ocd_command("read_memory 0xe000ed00 32 1; [target current] curstate", timeout=2)

    def find_probe(self):
        bluetooth = self.transport.get() == "Bluetooth"
        find = discover_ble_probes if bluetooth else discover_wifi
        self.show_link("active", f"Đang tìm ESP32 qua {self.transport.get()}.", "probe")
        self.run_task("Đang tìm probe...", find, result=lambda found: self.select_probe(found, bluetooth),
                      failed=self.connection_failed)

    def select_probe(self, found, bluetooth):
        if not found:
            self.show_link("error", "Chưa tìm thấy ESP32. Kiểm tra chế độ probe hoặc nhập IP Wi-Fi.", "probe")
            self.connection_text.set("Chưa tìm thấy ESP32.")
            return
        if bluetooth:
            choices = [(f"{item.name} · {item.address}", item.address) for item in found]
        else:
            choices = [(f"{ip} · {mac}", (mac, ip)) for mac, ip in found]
        selected = self.choose_item("Chọn ESP32", [label for label, _ in choices])
        if selected is None:
            self.show_link("idle", "Chọn probe để kết nối.")
            self.connection_text.set("Chưa chọn ESP32.")
            return
        probe = dict(choices)[selected]
        if bluetooth:
            self.ble_address = probe
        else:
            self.mac, ip = probe
            self.ip.set(ip)
            self.last_ip = ip
        self.begin_connect()

    def choose_item(self, title, choices):
        if len(choices) == 1:
            return choices[0]
        options = "\n".join(f"{index}. {label}" for index, label in enumerate(choices, 1))
        selection = simpledialog.askinteger(title, options, minvalue=1, maxvalue=len(choices), parent=self.root)
        return choices[selection - 1] if selection is not None else None

    def use_ip(self, ip):
        self.transport.set("Wi-Fi")
        self.mac = ""
        self.ip.set(ip)
        self.last_ip = ip
        self.begin_connect()

    def choose_usb(self):
        ports = usb_probes()
        if ports:
            return self.choose_item("Chọn cổng USB của ESP32", ports)
        self.connection_text.set("Cắm cáp USB dữ liệu vào ESP32 rồi thử lại.")
        return None

    def find_usb(self):
        port = self.choose_usb()
        if port:
            self.run_task("Đang tìm IP qua USB...", usb_ip, port, result=self.use_ip)

    def setup_wifi(self):
        port = self.choose_usb()
        if not port:
            return
        ssid = simpledialog.askstring("Wi-Fi của probe", "Tên Wi-Fi 2,4 GHz", parent=self.root)
        if ssid is None:
            return
        password = simpledialog.askstring("Wi-Fi của probe", "Mật khẩu WPA2", show="*", parent=self.root)
        if password is not None:
            self.run_task("Đang cài Wi-Fi và lấy IP...", configure_wifi, port, ssid, password, result=self.use_ip)

    def change_usb_mode(self):
        port = self.choose_usb()
        if not port:
            return
        mode = self.transport.get()
        if messagebox.askyesno("Đổi chế độ ESP", f"Đặt ESP32 ở chế độ {mode} và khởi động lại?", parent=self.root):
            self.run_task("Đang đổi chế độ ESP32...", set_usb_mode, port, "ble" if mode == "Bluetooth" else "wifi",
                          result=lambda _: self.connection_text.set(f"ESP32 khởi động lại ở chế độ {mode}. Bấm Tìm probe."))

    @staticmethod
    def ocd_command(command, timeout=3):
        with OpenOcd(timeout=timeout) as link:
            return link._call(command)

    def run_control(self, action):
        self.health = None
        stopping = action == "halt"
        command = "halt 2000" if stopping else "resume"
        label = "Đang dừng STM32..." if stopping else "Đang cho STM32 chạy tiếp..."
        self.run_task(label, self.ocd_command, f"{command}; [target current] curstate", 5,
                      result=self.control_done)

    def control_done(self, state):
        self.set_core_state(state)
        self.next_check = monotonic() + CHECK_INTERVAL
        self.connection_text.set(self.core_text.get())

    def program(self):
        if not self.elf_path or not self.can_use_target():
            return
        path = Path(self.elf_path)
        if not path.is_file():
            messagebox.showerror("Không tìm thấy ELF", str(path), parent=self.root)
            return
        session = self.write_session()
        confirmed = messagebox.askyesno("Nạp STM32", f"Ghi {path.name} vào flash, kiểm tra rồi reset chạy?", parent=self.root)
        if confirmed and self.same_session(session):
            self.verified = None
            self.close_rtt_window()
            self.run_task("Đang nạp ELF và verify...", self.server.program, path,
                          result=lambda _: self.connection_text.set("Đã nạp, verify và reset STM32."))

    def verify_current_elf(self, link, force=False):
        if not self.elf_path:
            raise RuntimeError("Mở ELF khớp với firmware STM32 đang chạy.")
        version = self.elf_signature(self.elf_path)
        if version != self.elf_version:
            self.verified = None
            raise RuntimeError("ELF đã thay đổi trên đĩa. Mở lại ELF để cập nhật địa chỉ biến.")
        if not force and self.verified == version:
            return None
        self.verified = None
        self.verify_progress = (0, 0)
        count = verify_elf(self.elf_path, link, cancelled=self.verify_cancel.is_set, progress=self.track_progress)
        if self.elf_signature(self.elf_path) != version:
            raise RuntimeError("ELF đã thay đổi trong lúc kiểm tra. Mở lại ELF rồi thử lại.")
        self.verified = version
        return count

    def track_progress(self, done, total):
        self.verify_progress = done, total

    def check_elf(self):
        def verify():
            with OpenOcd(self.port, timeout=8) as link:
                return self.verify_current_elf(link, force=True)

        self.run_task("Đang so khớp ELF với flash STM32...", verify, result=self.elf_matched)

    def elf_matched(self, count):
        self.connection_text.set(f"ELF khớp {count:,} byte trong flash STM32.")

    def write(self, symbol, raw):
        with OpenOcd(self.port, timeout=8) as link:
            if not symbol.manual:
                self.verify_current_elf(link)
            return link.write(symbol, raw)


    def save_preset(self):
        options = []
        for row in self.descendants("", expanded_only=True):
            if row in self.raw_values:
                symbol, kind = self.watch[row]
                if symbol.writable:
                    options.append((self.paths[row], symbol, kind, self.raw_values[row]))
        if not options:
            self.connection_text.set("Mở nhánh cần lưu và đợi app đọc giá trị.")
            return
        dialog = PresetDialog(self.root, options)
        if dialog.result is None:
            return
        name, values = dialog.result
        if name in self.presets and not messagebox.askyesno(
            "Lưu tuning", f"Ghi đè preset {name}?", parent=self.root
        ):
            return
        self.presets = {**self.presets, name: values}
        if self.save_settings():
            self.connection_text.set(f"Đã lưu {len(values)} giá trị vào preset {name}.")

    def choose_preset(self):
        session = self.write_session()
        name = self.choose_item("Chọn preset tuning", sorted(self.presets))
        if name is None:
            return
        try:
            values = self.preset_values(self.presets[name])
        except (KeyError, ValueError, TypeError) as error:
            messagebox.showerror("Preset không hợp lệ", str(error), parent=self.root)
            return
        confirmed = messagebox.askyesno("Áp tuning", f"Ghi {len(values)} giá trị RAM từ preset {name}?", parent=self.root)
        if confirmed and self.same_session(session):
            self.run_task("Đang áp tuning qua SWD...", self.write_preset, values,
                          result=lambda count: self.connection_text.set(f"Đã ghi {count} giá trị từ {name}."))

    def preset_values(self, entries):
        if not isinstance(entries, list) or not entries:
            raise ValueError("Preset rỗng hoặc sai định dạng.")
        watched = {(symbol.manual, symbol.label, symbol.address, symbol.size): symbol
                   for symbol, _ in self.watch.values()}
        values = []
        for item in entries:
            key = (item["manual"], item["name"], item["address"], item["size"])
            symbol = watched.get(key)
            if symbol is None or not symbol.writable:
                raise ValueError(f"Thêm lại {item['name']} với địa chỉ và kích thước gốc vào Watch.")
            if item.get("kind", symbol.kind) != symbol.kind:
                raise ValueError(f"Kiểu dữ liệu của {symbol.name} đã thay đổi. Lưu lại preset từ ELF hiện tại.")
            raw = item["raw"]
            if type(raw) is not int or not 0 <= raw < (1 << (symbol.size * 8)):
                raise ValueError(f"Giá trị lưu của {symbol.name} nằm ngoài kích thước kiểu dữ liệu.")
            values.append((symbol, raw))
        return values

    def write_preset(self, values):
        with OpenOcd(self.port, timeout=8) as link:
            if any(not symbol.manual for symbol, _ in values):
                self.verify_current_elf(link)
            for count, (symbol, raw) in enumerate(values):
                try:
                    observed = link.write(symbol, raw)
                except (OSError, RuntimeError, ValueError) as error:
                    raise RuntimeError(f"Đã xử lý {count}/{len(values)} biến. {symbol.name} · {error}") from error
                if observed != raw:
                    raise RuntimeError(f"Đã xác nhận {count}/{len(values)} biến. Đã ghi {symbol.name} 0x{raw:X}, "
                                       f"đọc lại 0x{observed:X}. Kiểm tra firmware đang cập nhật biến này.")
        return len(values)

    def open_plot(self):
        if self.plot is None or not self.plot.winfo_exists():
            self.plot = PlotWindow(self.root)
        self.sync_plot()
        self.plot.lift()

    def sync_plot(self):
        if self.plot is not None and self.plot.winfo_exists():
            self.plot.sync_series({
                row: f"{self.paths[row]} (0x{symbol.address:08X})" for row, (symbol, _) in self.watch.items()
            })

    def on_read(self, values):
        if self.plot is None or not self.plot.winfo_exists():
            return
        self.sync_plot()
        sample = {}
        for row, raw in values.items():
            if row in self.watch:
                symbol, kind = self.watch[row]
                sample[row] = numeric_value(raw, symbol.size, kind)
        if sample:
            self.plot.add_sample(sample)

    def open_rtt(self):
        if self.rtt_window is not None and self.rtt_window.winfo_exists():
            self.rtt_window.lift()
            return
        default = None
        if self.elf_path:
            try:
                default = rtt_address(self.elf_path)
            except (OSError, ValueError, RuntimeError) as error:
                self.connection_text.set(f"Lỗi đọc địa chỉ RTT từ ELF · {error}")
        address = simpledialog.askstring(
            "Log SEGGER RTT", "Địa chỉ control block RTT trong SRAM", parent=self.root,
            initialvalue=f"0x{default:08X}" if default is not None else "",
        )
        if address is None:
            return
        channel = simpledialog.askinteger("Log SEGGER RTT", "Kênh log", parent=self.root, initialvalue=0,
                                          minvalue=0, maxvalue=31)
        if channel is None:
            return
        try:
            capture = RttCapture(int(address, 0), channel=channel, tcl_port=self.port)
        except ValueError as error:
            messagebox.showerror("Địa chỉ RTT không hợp lệ", str(error), parent=self.root)
            return
        if self.can_use_target():
            self.run_task("Đang tìm control block RTT...", self.start_rtt, capture, result=self.show_rtt)

    def start_rtt(self, capture):
        capture.start()
        self.rtt = capture
        return capture

    def show_rtt(self, capture):
        self.rtt_window = RttWindow(self.root, capture, self.stop_rtt_window)
        self.connection_text.set("Đang đọc log RTT.")

    def close_rtt_window(self):
        if self.rtt_window is not None and self.rtt_window.winfo_exists():
            self.rtt_window.stop()

    def stop_rtt_window(self, capture):
        self.rtt_window = None
        if not self.closing:
            self.executor.submit(self.stop_rtt, capture)

    def stop_rtt(self, capture):
        if self.rtt is capture:
            try:
                capture.stop()
            except (OSError, RuntimeError):
                pass
            finally:
                self.rtt = None

    def poll(self):
        self.update_link_progress()
        if self.verify_progress is not None:
            done, total = self.verify_progress
            progress = f" · {100 * done // total}%" if total else ""
            self.connection_text.set(f"Đang so khớp ELF với flash STM32{progress}...")
        if self.activity is not None and self.activity[0].done():
            task, callback, failed = self.activity
            self.activity = None
            self.verify_progress = None
            try:
                value = task.result()
                if callback and not self.closing:
                    callback(value)
            except Exception as error:
                if failed:
                    failed(error)
                else:
                    self.connection_text.set(f"Lỗi · {error}")

        if self.closing and self.activity is None and self.pending_write is None:
            try:
                self.stop_link()
            finally:
                super().close()
            return

        if self.pending_write is not None and self.pending_write[1].done() and self.verify_progress is not None:
            self.verify_progress = None
            self.connection_text.set(f"{self.transport.get()} · {self.link_status}")

        if self.keep_link and not self.closing:
            self.poll_link()
        self.sync_plot()
        super().poll()
        self.refresh_buttons()

    def poll_link(self):
        now = monotonic()
        if self.activity is not None or self.pending_write is not None:
            return
        if self.health is not None and self.health.done():
            try:
                self.set_core_state(self.health.result())
            except (OSError, ValueError, RuntimeError) as error:
                self.connection_failed(error)
                self.close_rtt_window()
                self.reset_values()
            self.health = None
        if not self.link_status:
            if now >= self.next_retry:
                self.begin_connect()
        elif self.health is None and now >= self.next_check:
            self.health = self.executor.submit(self.probe_alive)
            self.next_check = now + CHECK_INTERVAL

    def close(self):
        if not self.closing:
            self.save_settings()
            self.closing = True
            self.verify_cancel.set()
            self.connection_text.set("Đang hoàn tất thao tác trước khi đóng...")
            self.refresh_buttons()


def main():
    folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "STM32 Wireless Debug"
    folder.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.title("STM32 Wireless Debug 1.3")
    root.geometry("985x560")
    root.minsize(830, 400)
    DesktopApp(root, folder)
    root.mainloop()


if __name__ == "__main__":
    main()

