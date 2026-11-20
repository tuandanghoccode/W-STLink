"""Launch the packaged OpenOCD server and provision the probe through USB."""

import ipaddress
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from time import monotonic, sleep

import serial
from serial.tools.list_ports import comports

from live_memory import OpenOcd

RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TARGETS = {
    "STM32F0": "stm32f0x.cfg",
    "STM32F1": "stm32f1x.cfg",
    "STM32F2": "stm32f2x.cfg",
    "STM32F3": "stm32f3x.cfg",
    "STM32F4": "stm32f4x.cfg",
    "STM32F7": "stm32f7x.cfg",
    "STM32G0": "stm32g0x.cfg",
    "STM32G4": "stm32g4x.cfg",
    "STM32H7": "stm32h7x.cfg",
    "STM32L0": "stm32l0.cfg",
    "STM32L4": "stm32l4x.cfg",
    "STM32U5": "stm32u5x.cfg",
}


class ProbeServer:
    def __init__(self, log_path):
        self.log_path = log_path
        self.process = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, probe_ip, target, probe_port=4441, *, progress=None):
        probe_ip = str(ipaddress.IPv4Address(probe_ip))
        if not 1 <= probe_port <= 65535:
            raise ValueError("Cổng probe không hợp lệ.")
        target_script = TARGETS[target]
        self.stop()
        for port in (3333, 6666):
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            except OSError:
                continue
            with connection:
                raise RuntimeError(f"Cổng {port} đang được dùng. Đóng phiên OpenOCD hiện tại rồi thử lại.")

        tool_root = RESOURCES / "openocd"
        bundled = tool_root / "bin" / "openocd.exe"
        executable = bundled if bundled.is_file() else shutil.which("openocd.exe")
        if executable is None:
            raise RuntimeError("Không tìm thấy OpenOCD. Dùng bản .exe đã đóng gói.")

        args = [str(executable)]
        if bundled.is_file():
            args.extend(["-s", str(tool_root / "openocd" / "scripts")])
        args.extend([
            "-c", f"set PROBE_IP {probe_ip}",
            "-c", f"set PROBE_PORT {probe_port}",
            "-c", f"set TARGET_SCRIPT target/{target_script}",
            "-f", str(RESOURCES / "wireless.cfg"),
        ])
        if progress is not None:
            progress("openocd", "Đang khởi động và chờ OpenOCD sẵn sàng…")
        with self.log_path.open("wb") as log:
            self.process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        try:
            deadline = monotonic() + (35 if probe_port != 4441 else 16)
            reported_swd = False
            while monotonic() < deadline and self.running:
                try:
                    with OpenOcd() as link:
                        if progress is not None and not reported_swd:
                            reported_swd = True
                            progress("swd", "OpenOCD đã mở. Đang đọc CPUID của STM32 qua SWD…")
                        cpu_id = int(link._call("read_memory 0xe000ed00 32 1"), 0)
                    return f"Đã nối STM32 · CPUID 0x{cpu_id:08X} · GDB 3333"
                except OSError:
                    sleep(0.2)
            info = self.log_path.read_text(encoding="utf-8", errors="replace")
            errors = [line for line in info.splitlines() if "Error:" in line and "WSAGetLastError==10035" not in line]
            raise RuntimeError(errors[-1] if errors else "Không kết nối được probe. Kiểm tra IP và dây SWD.")
        except Exception:
            self.stop()
            raise

    def program(self, path):
        word = tcl_path(path)
        steps = (
            ("Reset và halt", "reset init"),
            ("Chọn tốc độ SWD", "adapter speed 100"),
            ("Ghi flash", f"flash write_image erase {word}"),
            ("Verify flash", f"verify_image {word}"),
            ("Reset chạy", "reset run"),
        )
        with OpenOcd(timeout=600) as link:
            for stage, command in steps:
                offset = self.log_path.stat().st_size
                try:
                    link._call(command)
                except (OSError, RuntimeError) as error:
                    detail = self._program_error(offset, error)
                    raise RuntimeError(f"{stage}: {detail}") from error

    def _program_error(self, offset, error):
        try:
            with self.log_path.open("rb") as log:
                log.seek(0, 2)
                log.seek(max(offset, log.tell() - 65536))
                lines = log.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        errors = [
            line.removeprefix("Error: ").strip()
            for line in lines
            if line.startswith("Error: ")
            and "WSAGetLastError==10035" not in line
            and "error reading header" not in line
        ]
        if errors:
            return " · ".join(errors[-2:])
        if str(error).strip() == "auto erase enabled":
            return "OpenOCD dừng trong lúc ghi. Xem openocd.log để biết thêm."
        return str(error)

    def stop(self):
        if self.running:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None


def discover_wifi(timeout=1.2, address=""):
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        connection.bind(("", 0))
        destinations = (address, "255.255.255.255") if address else ("255.255.255.255",)
        sent = False
        for destination in destinations:
            try:
                connection.sendto(b"STM32-WDEBUG/1 DISCOVER", (destination, 4443))
                sent = True
            except OSError as error:
                last_error = error
        if not sent:
            raise last_error
        deadline = monotonic() + timeout
        while (remaining := deadline - monotonic()) > 0:
            connection.settimeout(remaining)
            try:
                reply, (address, _) = connection.recvfrom(128)
            except TimeoutError:
                break
            match = re.fullmatch(rb"STM32-WDEBUG/1 ([A-Fa-f0-9]{12})", reply)
            if match:
                found[match[1].decode("ascii").upper()] = address
    return sorted(found.items())


def usb_probes():
    return sorted(port.device for port in comports() if (port.vid, port.pid) == (0x303A, 0x1001))


def _usb_command(port, message, marker=None, wait=1.6):
    connection = serial.Serial(port=None, baudrate=115200, timeout=0.15, write_timeout=1)
    connection.dtr, connection.rts, connection.port = False, False, port
    with connection:
        connection.write(message.encode("utf-8") + b"\r")
        connection.flush()
        deadline = monotonic() + wait
        reply = bytearray()
        while monotonic() < deadline:
            reply.extend(connection.read(connection.in_waiting or 1))
            if marker and marker in reply:
                break
    return bytes(reply)


def usb_ip(port):
    status = _usb_command(port, "status")
    match = re.search(rb"IP address: ([0-9.]+)", status)
    if match:
        address = ipaddress.IPv4Address(match[1].decode("ascii"))
        if not address.is_unspecified:
            return str(address)
    raise RuntimeError(f"ESP32 ở {port} chưa báo IP. Cài Wi-Fi cho probe rồi thử lại.")


def _console_word(value):
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Tên Wi-Fi và mật khẩu phải nằm trên một dòng.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def configure_wifi(port, ssid, password):
    if not 1 <= len(ssid.encode("utf-8")) <= 31 or not 8 <= len(password.encode("utf-8")) <= 63:
        raise ValueError("Tên Wi-Fi tối đa 31 byte và mật khẩu WPA2 cần 8–63 byte.")
    confirmation = b"WiFi credentials saved successfully."
    command = f"wifi {_console_word(ssid)} {_console_word(password)} wpa2"
    if confirmation not in _usb_command(port, command, confirmation):
        raise RuntimeError(f"ESP32 ở {port} chưa xác nhận lưu Wi-Fi.")
    _usb_command(port, "reboot", wait=0)
    deadline = monotonic() + 25
    while monotonic() < deadline:
        try:
            return usb_ip(port)
        except (OSError, RuntimeError):
            sleep(0.4)
    raise RuntimeError("ESP32 chưa nhận IP. Kiểm tra Wi-Fi 2,4 GHz và mật khẩu.")


def set_usb_mode(port, mode):
    if mode not in ("wifi", "ble"):
        raise ValueError("Chế độ ESP32 không hợp lệ.")
    confirmation = f"Link mode saved: {mode}.".encode("ascii")
    if confirmation not in _usb_command(port, f"link {mode}", confirmation):
        raise RuntimeError("ESP32 chưa xác nhận chế độ. Cập nhật firmware probe rồi thử lại.")
    _usb_command(port, "reboot", wait=0)


def tcl_path(path):
    value = Path(path).resolve().as_posix()
    escaped = re.sub(r'([\\$\[\]{}"])', r"\\\1", value)
    return f'"{escaped}"'
