"""Run OpenOCD's CMSIS-DAP TCP transport over the probe's BLE GATT service."""

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass
import struct
from threading import Event, Thread

from bleak import BleakClient, BleakScanner


SERVICE_UUID = "8f3f0001-58c9-4b26-a4c1-327a8c1a8dd0"
RX_UUID = "8f3f0002-58c9-4b26-a4c1-327a8c1a8dd0"
TX_UUID = "8f3f0003-58c9-4b26-a4c1-327a8c1a8dd0"
HEADER = struct.Struct("<4sHBB")
MAX_PAYLOAD = 4096


class BleBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BleProbe:
    address: str
    name: str


async def _scan(timeout):
    devices = await BleakScanner.discover(
        timeout=timeout, return_adv=True, service_uuids=[SERVICE_UUID]
    )
    probes = [
        (device, BleProbe(device.address, advertisement.local_name or device.name or device.address))
        for device, advertisement in devices.values()
    ]
    return sorted(probes, key=lambda item: (item[1].name, item[1].address))


def discover_ble_probes(timeout=5.0):
    """Scan for probe advertisements. Call from the desktop background worker."""
    return [probe for _, probe in asyncio.run(_scan(timeout))]


def _payload_size(data, expected_kind):
    magic, size, kind, reserved = HEADER.unpack_from(data)
    if magic != b"DAP\0" or kind != expected_kind or reserved or not 0 < size <= MAX_PAYLOAD:
        raise BleBridgeError("Khung CMSIS-DAP BLE không hợp lệ.")
    return size


class BleBridge:
    def __init__(self, address=None, *, scan_timeout=5.0, connect_timeout=30.0, request_timeout=8.0):
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

    def start(self, *, progress=None):
        """Connect to the probe and return the assigned IPv4 loopback TCP port."""
        if self._thread is not None and self._thread.is_alive():
            raise BleBridgeError("Kết nối Bluetooth đang mở hoặc đang đóng.")
        self.last_error = None
        self.probe_address = None
        self._ready = Future()
        self._stop_requested.clear()
        self._thread = Thread(target=self._run, args=(progress,), name="STM32 Bluetooth", daemon=True)
        self._thread.start()
        try:
            return self._ready.result(timeout=self.scan_timeout + self.connect_timeout + 12)
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
            message = self.last_error or str(exc).strip() or "Kết nối probe Bluetooth đã đóng."
            self.last_error = message
            if not self._ready.done():
                self._ready.set_exception(BleBridgeError(message))
        finally:
            self._port = None
            self._loop = None

    async def _select_device(self):
        probes = await asyncio.wait_for(_scan(self.scan_timeout), timeout=self.scan_timeout + 2)
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
                progress("probe", f"Đã tìm thấy {self.probe_address}. Đang ghép cặp và kết nối Bluetooth…")
            client = BleakClient(
                device, disconnected_callback=self._disconnected,
                services=[SERVICE_UUID], pair=True, timeout=self.connect_timeout,
            )
            self._client = client
            try:
                await asyncio.wait_for(client.connect(), timeout=self.connect_timeout)
                if progress is not None:
                    progress("probe", "Đã kết nối Bluetooth. Đang đăng ký nhận phản hồi từ probe…")
                await asyncio.wait_for(client.start_notify(TX_UUID, self._notify), timeout=5)
            except TimeoutError as exc:
                raise BleBridgeError("Hết thời gian ghép cặp hoặc kết nối probe Bluetooth.") from exc
            if self._shutdown.is_set():
                raise BleBridgeError(self.last_error or "Kết nối probe Bluetooth đã đóng.")
            server = await asyncio.start_server(self._handle_tcp, "127.0.0.1", 0)
            if self._shutdown.is_set():
                raise BleBridgeError(self.last_error or "Kết nối probe Bluetooth đã đóng.")
            self._port = server.sockets[0].getsockname()[1]
            if progress is not None:
                progress("probe", "Probe Bluetooth đã sẵn sàng.")
            self._ready.set_result(self._port)
            await self._shutdown.wait()
        finally:
            self._shutdown.set()
            self._port = None
            if not self._ready.done() and self._stop_requested.is_set():
                self._ready.set_exception(BleBridgeError("Kết nối probe Bluetooth đã đóng."))
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
            self._fail("Probe Bluetooth gửi phản hồi ngoài phiên CMSIS-DAP.")
            return
        self._response_size += len(fragment)
        if self._response_size > HEADER.size + MAX_PAYLOAD:
            self._fail("Phản hồi của probe Bluetooth vượt kích thước CMSIS-DAP.")
            return
        self._response.put_nowait(bytes(fragment))

    async def _exchange(self, frame):
        self._response = asyncio.Queue()
        self._response_size = 0
        try:
            chunk_size = min(512, self._client.mtu_size - 3)
            if chunk_size <= 0:
                raise BleBridgeError("MTU của kết nối Bluetooth không hợp lệ.")
            for offset in range(0, len(frame), chunk_size):
                await self._client.write_gatt_char(
                    RX_UUID, frame[offset:offset + chunk_size], response=True
                )
            result = bytearray()
            while len(result) < HEADER.size:
                result.extend(await self._response.get())
            expected = HEADER.size + _payload_size(result, expected_kind=2)
            while len(result) < expected:
                result.extend(await self._response.get())
            if len(result) != expected or not self._response.empty():
                raise BleBridgeError("Probe Bluetooth gửi dư dữ liệu CMSIS-DAP.")
            return result
        finally:
            self._response = None

    async def _handle_tcp(self, reader, writer):
        if self._peer is not None or self._shutdown.is_set():
            writer.close()
            return
        self._peer = asyncio.current_task()
        used = False
        try:
            while True:
                header = await reader.readexactly(HEADER.size)
                size = _payload_size(header, expected_kind=1)
                used = True
                payload = await asyncio.wait_for(reader.readexactly(size), timeout=self.request_timeout)
                response = await asyncio.wait_for(self._exchange(header + payload), timeout=self.request_timeout)
                writer.write(response)
                await asyncio.wait_for(writer.drain(), timeout=self.request_timeout)
        except asyncio.IncompleteReadError as exc:
            if used or exc.partial:
                self._fail("Kết nối OpenOCD qua Bluetooth đã đóng.")
        except TimeoutError:
            self._fail("Hết thời gian truyền gói CMSIS-DAP qua Bluetooth.")
        except Exception as exc:
            self._fail(str(exc).strip() or "Lỗi truyền gói CMSIS-DAP qua Bluetooth.")
        finally:
            writer.close()
            try:
                with suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1)
            finally:
                self._peer = None
