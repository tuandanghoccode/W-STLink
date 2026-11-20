"""Host-side loopback TCP bridge to the BLE CMSIS-DAP probe."""

import asyncio
import logging
import struct
import sys
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass
from math import ceil
from threading import Event, Thread
from time import monotonic

from bleak import BleakClient, BleakScanner

LOGGER = logging.getLogger("stm32-wireless-debug.ble")
SERVICE_UUID = "8F3F0001-58C9-4B26-A4C1-327A8C1A8DD0"
RX_UUID = "8F3F0002-58C9-4B26-A4C1-327A8C1A8DD0"  # Probe RX (Host writes to probe)
TX_UUID = "8F3F0003-58C9-4B26-A4C1-327A8C1A8DD0"  # Probe TX (Probe notifies host)
WIFI_CFG_UUID = "8F3F0004-58C9-4B26-A4C1-327A8C1A8DD0"
HEADER = struct.Struct("<4sHBB")
HEADER_SIZE = HEADER.size
MAX_PAYLOAD = 1024


class BleBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BleProbe:
    address: str
    name: str


async def _scan(timeout):
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    target = SERVICE_UUID.casefold()
    probes = []
    for device, advertisement in devices.values():
        uuids = [str(u).casefold() for u in (advertisement.service_uuids or [])]
        name = advertisement.local_name or device.name or ""
        if target in uuids or name.startswith("STM32-WDEBUG"):
            probes.append((device, BleProbe(device.address, name or device.address)))
    return sorted(probes, key=lambda item: (item[1].name, item[1].address))


def discover_ble_probes(timeout=5.0):
    """Scan for probe advertisements. Call from the desktop background worker."""
    return [probe for _, probe in asyncio.run(_scan(timeout))]


def _payload_size(data, expected_kind):
    magic, size, kind, reserved = HEADER.unpack_from(data)
    if (
        magic != b"DAP\0"
        or kind != expected_kind
        or reserved != 0
        or not (0 < size <= MAX_PAYLOAD)
    ):
        raise BleBridgeError("Khung CMSIS-DAP BLE không hợp lệ.")
    return size


class BleBridge:
    def __init__(
        self,
        address=None,
        *,
        scan_timeout=5.0,
        connect_timeout=30.0,
        request_timeout=8.0,
    ):
        self.address = address
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.last_error = None
        self.probe_address = None
        self._thread = None
        self._loop = None
        self._port = None
        self._stop_requested = Event()

    @property
    def is_running(self):
        return self._port is not None

    @property
    def port(self):
        return self._port

    def start(self, *, progress=None):
        """Connect to the probe and return the assigned IPv4 loopback TCP port."""
        if self._thread is not None and self._thread.is_alive():
            raise BleBridgeError("Kết nối Bluetooth đang mở hoặc đang đóng.")
        self.last_error = None
        self.probe_address = None
        self._ready = Future()
        self._stop_requested.clear()
        self._thread = Thread(
            target=self._run, args=(progress,), name="STM32 Bluetooth", daemon=True
        )
        self._thread.start()
        try:
            return self._ready.result(
                timeout=self.scan_timeout + self.connect_timeout + 12
            )
        except FutureTimeout as exc:
            self.stop()
            raise BleBridgeError("Hết thời gian kết nối probe Bluetooth.") from exc
        except BleBridgeError:
            self.stop()
            raise

    def stop(self):
        """Close the local listener and disconnect from the probe."""
        self._stop_requested.set()
        loop = self._loop
        if loop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._stop_on_loop)
        if self._thread is not None:
            self._thread.join(timeout=7)

    def _stop_on_loop(self):
        if not self._ready.done() and not self._shutdown.is_set():
            self._service_task.cancel()
        self._shutdown.set()

    def _run(self, progress):
        try:
            asyncio.run(self._serve(progress))
        except (Exception, asyncio.CancelledError) as exc:
            message = (
                self.last_error
                or str(exc).strip()
                or "Kết nối probe Bluetooth đã đóng."
            )
            self.last_error = message
            if not self._ready.done():
                self._ready.set_exception(BleBridgeError(message))
        finally:
            self._port = None
            self._loop = None

    async def _select_device(self):
        if self.address:
            address = self.address.strip().casefold().replace("-", ":")
            try:
                device = await BleakScanner.find_device_by_address(address, timeout=2.5)
                if device:
                    self.probe_address = device.address
                    return device
            except Exception:
                pass
        probes = await asyncio.wait_for(
            _scan(self.scan_timeout), timeout=self.scan_timeout + 2
        )
        if self.address:
            address = self.address.strip().casefold().replace("-", ":")
            probes = [item for item in probes if item[1].address.casefold() == address]
        if not probes:
            raise BleBridgeError("Không tìm thấy probe Bluetooth đang quảng bá.")
        if len(probes) > 1:
            raise BleBridgeError("Có nhiều probe Bluetooth. Hãy chọn một probe.")
        device, probe = probes[0]
        self.probe_address = probe.address
        return device

    async def set_wifi(self, ssid, password):
        if self._client is None or not self._client.is_connected:
            raise BleBridgeError("Chưa kết nối tới probe Bluetooth.")
        payload = f"{ssid}\n{password}".encode("utf-8")
        await self._client.write_gatt_char(WIFI_CFG_UUID, payload, response=True)
        resp = await self._client.read_gatt_char(WIFI_CFG_UUID)
        return resp.decode("utf-8", errors="replace")

    async def _serve(self, progress):
        self._shutdown = asyncio.Event()
        self._service_task = asyncio.current_task()
        self._peer = None
        self._response = None
        self._loop = asyncio.get_running_loop()
        server = None
        client = None
        try:
            if self._stop_requested.is_set():
                return
            if progress is not None:
                progress("probe", "Đang tìm probe Bluetooth…")
            device = await self._select_device()
            if progress is not None:
                progress(
                    "probe",
                    f"Đã tìm thấy {self.probe_address}. Đang ghép cặp và kết nối Bluetooth…",
                )
            client = BleakClient(
                device,
                disconnected_callback=self._disconnected,
                services=[SERVICE_UUID],
                pair=False,
                timeout=self.connect_timeout,
            )
            self._client = client
            try:
                await asyncio.wait_for(client.connect(), timeout=self.connect_timeout)
                if hasattr(client._backend, "_acquire_mtu"):
                    with suppress(Exception):
                        await client._backend._acquire_mtu()
                if progress is not None:
                    progress(
                        "probe",
                        "Đã kết nối Bluetooth. Đang đăng ký nhận phản hồi từ probe…",
                    )
                await asyncio.wait_for(
                    client.start_notify(TX_UUID, self._notify), timeout=5
                )
            except TimeoutError as exc:
                raise BleBridgeError(
                    "Hết thời gian ghép cặp hoặc kết nối probe Bluetooth."
                ) from exc
            if self._shutdown.is_set():
                raise BleBridgeError(
                    self.last_error or "Kết nối probe Bluetooth đã đóng."
                )
            server = await asyncio.start_server(self._handle_tcp, "127.0.0.1", 0)
            if self._shutdown.is_set():
                raise BleBridgeError(
                    self.last_error or "Kết nối probe Bluetooth đã đóng."
                )
            self._port = server.sockets[0].getsockname()[1]
            if progress is not None:
                progress("probe", "Probe Bluetooth đã sẵn sàng.")
            self._ready.set_result(self._port)
            await self._shutdown.wait()
        finally:
            self._shutdown.set()
            self._port = None
            if not self._ready.done() and self._stop_requested.is_set():
                self._ready.set_exception(
                    BleBridgeError("Kết nối probe Bluetooth đã đóng.")
                )
            try:
                if server is not None:
                    server.close()
                    if self._peer is not None:
                        self._peer.cancel()
                        with suppress(asyncio.CancelledError):
                            await self._peer
                    with suppress(TimeoutError):
                        await asyncio.wait_for(server.wait_closed(), timeout=2)
            finally:
                if client is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(client.disconnect(), timeout=4)

    def _fail(self, message):
        if not self._shutdown.is_set():
            self.last_error = message
            self._port = None
            self._shutdown.set()

    def _disconnected(self, _client):
        self._fail("Đã mất kết nối với probe Bluetooth.")

    def _notify(self, _characteristic, fragment):
        if self._shutdown.is_set() or not fragment:
            return
        if self._response is None:
            self._expected = None
            self._response = bytearray()
        self._response.extend(fragment)
        if self._expected is None and len(self._response) >= HEADER_SIZE:
            try:
                self._expected = HEADER_SIZE + _payload_size(self._response, 2)
            except Exception:
                self._response.clear()
                self._expected = None
                return
        if self._expected is not None and len(self._response) >= self._expected:
            packet = bytes(self._response[: self._expected])
            self._response = bytearray(self._response[self._expected :])
            self._expected = None
            if not self._packet_ready.done():
                self._packet_ready.set_result(packet)

    async def _handle_tcp(self, reader, writer):
        if self._peer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return
        self._peer = asyncio.current_task()
        try:
            while not self._shutdown.is_set():
                header = await reader.readexactly(HEADER_SIZE)
                size = _payload_size(header, 1)
                payload = await reader.readexactly(size)
                self._packet_ready = self._loop.create_future()
                packet = header + payload
                chunk_size = max(20, self._client.mtu_size - 3)
                for index in range(ceil(len(packet) / chunk_size)):
                    offset = index * chunk_size
                    chunk = packet[offset : offset + chunk_size]
                    await self._client.write_gatt_char(RX_UUID, chunk, response=False)
                try:
                    reply = await asyncio.wait_for(
                        self._packet_ready, timeout=self.request_timeout
                    )
                except TimeoutError as exc:
                    raise BleBridgeError(
                        "Hết thời gian chờ phản hồi từ probe Bluetooth."
                    ) from exc
                writer.write(reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail(str(exc).strip() or "Mất kết nối với probe Bluetooth.")
        finally:
            self._peer = None
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()


async def configure_wifi_ble(address, ssid, password, timeout=12.0):
    """Configure probe Wi-Fi credentials over BLE."""
    bridge = BleBridge(address=address, connect_timeout=timeout)
    device = await bridge._select_device()
    client = BleakClient(device, timeout=timeout)
    async with client:
        payload = f"{ssid}\n{password}".encode("utf-8")
        await client.write_gatt_char(WIFI_CFG_UUID, payload, response=True)
        try:
            resp = await client.read_gatt_char(WIFI_CFG_UUID)
            return resp.decode("utf-8", errors="replace").strip()
        except Exception:
            return "OK"
