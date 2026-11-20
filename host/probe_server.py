import ipaddress
import json
import logging
import re
import shutil
import socket
import subprocess
import sys
from collections import OrderedDict
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import serial
from live_memory import OpenOcd
from serial.tools.list_ports import comports

LOGGER = logging.getLogger("stm32-wireless-debug")
RESOURCES = Path(__file__).resolve().parent
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
                conn = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                conn.close()
            except OSError:
                continue
            self._cleanup_stale_openocd(port)
            sleep(0.4)
            try:
                conn2 = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                conn2.close()
                raise RuntimeError(
                    f"Cổng {port} đang được dùng. Đóng phiên OpenOCD hiện tại rồi thử lại."
                )
            except OSError:
                pass

        tool_root = RESOURCES / "openocd"
        bin_name = "openocd.exe" if sys.platform == "win32" else "openocd"
        bundled = tool_root / "bin" / bin_name
        if not bundled.is_file():
            fallback_vendor = (
                RESOURCES / "build" / "xpack-openocd-0.12.0-7" / "bin" / bin_name
            )
            if fallback_vendor.is_file():
                tool_root = RESOURCES / "build" / "xpack-openocd-0.12.0-7"
                bundled = fallback_vendor

        executable = (
            bundled
            if bundled.is_file()
            else (shutil.which(bin_name) or shutil.which("openocd"))
        )
        if executable is None:
            raise RuntimeError(
                "Không tìm thấy OpenOCD. Dùng bản đã đóng gói hoặc cài đặt openocd."
            )

        if bundled.is_file() and sys.platform != "win32":
            try:
                bundled.chmod(bundled.stat().st_mode | 0o755)
            except OSError:
                pass

        args = [str(executable)]
        if bundled.is_file():
            args.extend(["-s", str(tool_root / "openocd" / "scripts")])
        args.extend(
            [
                "-c",
                f"set PROBE_IP {probe_ip}",
                "-c",
                f"set PROBE_PORT {probe_port}",
                "-c",
                f"set TARGET_SCRIPT target/{target_script}",
                "-f",
                str(RESOURCES / "wireless.cfg"),
            ]
        )
        if progress is not None:
            progress("openocd", "Đang khởi động và chờ OpenOCD sẵn sàng…")
        with self.log_path.open("wb") as log:
            popen_kwargs = {
                "args": args,
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": subprocess.STDOUT,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = subprocess.Popen(**popen_kwargs)
        ready = False
        deadline = monotonic() + 15
        while monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                raise RuntimeError(self.read_log() or f"OpenOCD thoát với mã {code}.")
            try:
                with socket.create_connection(("127.0.0.1", 6666), timeout=0.2):
                    ready = True
                    break
            except OSError:
                sleep(0.1)
        if not ready:
            self.stop()
            raise RuntimeError(self.read_log() or "Hết thời gian chờ OpenOCD sẵn sàng.")

    def start_wifi(self, probe_ip, target, probe_port=4441, *, progress=None):
        self.start(probe_ip, target, probe_port, progress=progress)

    def start_ble(self, loopback_port, target, gdb_port=3333, *, progress=None):
        self.start("127.0.0.1", target, loopback_port, progress=progress)

    def read_log(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def wait_target(self, port=6666, timeout=12, *, progress=None):
        if progress is not None:
            progress("target", "Đang kết nối SWD và kiểm tra chip target…")
        deadline = monotonic() + timeout
        last_error = "Hết thời gian chờ target STM32."
        while monotonic() < deadline:
            if not self.running:
                raise RuntimeError(self.read_log() or "OpenOCD đã dừng.")
            try:
                with OpenOcd(port, timeout=4.0) as ocd:
                    ocd._call("catch {poll} _res; set _res")
                    state = ocd._call('set _res ""; catch {[target current] curstate} _res; set _res').strip()
                    if state in ("halted", "running", "reset"):
                        return
                    if state and state != "unknown":
                        last_error = f"Target ở trạng thái: {state}."
                    else:
                        last_error = "Target chưa phản hồi qua SWD."
            except Exception as error:
                last_error = str(error)
            sleep(0.5)
        self.stop()
        raise RuntimeError(f"Không phát hiện được target STM32 qua SWD · {last_error}")

    @staticmethod
    def _cleanup_stale_openocd(port=None):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/IM", "openocd.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                if port:
                    subprocess.run(
                        ["fuser", "-k", "-9", f"{port}/tcp"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                subprocess.run(
                    ["pkill", "-9", "-f", "openocd"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

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
        for target in destinations:
            try:
                connection.sendto(b"STM32-WDEBUG/1 DISCOVER\n", (target, 4440))
            except OSError:
                continue
        connection.settimeout(0.3)
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            try:
                reply, (address, _) = connection.recvfrom(256)
            except TimeoutError:
                break
            match = re.fullmatch(rb"STM32-WDEBUG/1 ([A-Fa-f0-9]{12})", reply)
            if match:
                found[match[1].decode("ascii").upper()] = address
    return sorted(found.items())


def usb_probes():
    return sorted(
        port.device for port in comports() if (port.vid, port.pid) == (0x303A, 0x1001)
    )


def _usb_command(port, message, marker=None, wait=1.6):
    connection = serial.Serial(port=port, baudrate=115200, timeout=0.2, write_timeout=1)
    with connection:
        sleep(0.05)
        try:
            connection.reset_input_buffer()
        except Exception:
            pass
        connection.write(b"\r\n" + message.encode("utf-8") + b"\r\n")
        connection.flush()
        deadline = monotonic() + wait
        reply = bytearray()
        while monotonic() < deadline:
            waiting = connection.in_waiting
            if waiting:
                reply.extend(connection.read(waiting))
                if marker and marker in reply:
                    break
            else:
                sleep(0.05)
    return bytes(reply)


def get_usb_mode(port):
    reply = _usb_command(port, "link", wait=0.6).decode("utf-8", errors="replace")
    match = re.search(r"Link mode:\s*([a-zA-Z0-9_-]+)", reply)
    if match:
        return match.group(1).lower()
    return None


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
    if (
        not 1 <= len(ssid.encode("utf-8")) <= 31
        or not 8 <= len(password.encode("utf-8")) <= 63
    ):
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
        raise RuntimeError(
            "ESP32 chưa xác nhận chế độ. Cập nhật firmware probe rồi thử lại."
        )
    _usb_command(port, "reboot", wait=0)


def tcl_path(path):
    value = Path(path).resolve().as_posix()
    escaped = re.sub(r'([\\$\[\]{}"])', r"\\\1", value)
    return f'"{escaped}"'
