# STM32 Wireless Debug 1.3

Mở [STM32 Wireless Debug 1.3.exe](<../dist/STM32 Wireless Debug 1.3.exe>) trên Windows x64. App mang theo Python và OpenOCD. Đóng bản cũ để giải phóng cổng GDB và Tcl. App dùng lại profile, ELF và danh sách Watch đã lưu.

## Kết nối

- **Wi-Fi** dùng mạng hiện tại của máy tính. Nhập IP của ESP32 đang dùng hoặc bấm **Tìm IP qua USB**. Với firmware ESP32 mới, **Tìm probe** dò trên cùng mạng LAN và app nhớ MAC để tìm lại khi DHCP đổi IP. **Cài Wi-Fi** cấu hình WPA2 2,4 GHz qua USB.
- **Bluetooth** kết nối thẳng ESP32-S3 với Windows và giữ Wi-Fi đang dùng trên máy tính. Cập nhật firmware ESP32 một lần theo [hướng dẫn nạp](../firmware/README.md#nạp-firmware-vào-supermini). Chọn **Bluetooth** trong app. Cắm USB, bấm **Đổi chế độ ESP**, đợi ESP khởi động lại rồi bấm **Tìm probe**. Chấp nhận yêu cầu ghép cặp của Windows nếu hiện. Có thể cấp nguồn bằng USB trong khi dữ liệu debug đi qua BLE.
- App thử kết nối lại nếu phiên SWD bị mất. Bấm **Ngắt** để dừng thử. Khi kết nối lại, app tiếp tục Watch. Lệnh ghi và lệnh nạp do người dùng xác nhận riêng.
- Hàng tiến trình hiển thị bước chuẩn bị ESP32, mở OpenOCD, kiểm tra SWD và trạng thái chạy/dừng của STM32.
- STM32CubeIDE hoặc GDB có thể dùng server tại `127.0.0.1:3333`. Bảng Watch dùng Tcl tại `127.0.0.1:6666`.

## Theo dõi firmware đang chạy

| Tính năng | Cách dùng |
| --- | --- |
| Watch theo tên | **Mở ELF** đúng với firmware đang chạy, chọn biến global hoặc static trong SRAM rồi bấm **Thêm biến**. Bấm đúp ô giá trị để ghi và đọc lại trong lúc chương trình chạy. |
| Watch struct | Thêm biến struct như biến thường, mở mũi tên để xem trường số, struct lồng hoặc mảng cố định. App đọc các nhánh đang mở. Cần ELF có thông tin DWARF để lấy layout. Các trường const, trường lệch căn hàng và thành viên union ở chế độ chỉ đọc. Phạm vi phiên bản này là trường số 1, 2 hoặc 4 byte, tối đa 32 phần tử mỗi chiều mảng và 256 nút mỗi biến. |
| Watch theo địa chỉ | Bấm **Thêm địa chỉ**, nhập địa chỉ SRAM từ file `.map` hoặc tài liệu firmware và chọn kiểu `uint8/16/32`, `int8/16/32` hoặc `float32`. Dùng được khi STM32 đã nạp `.bin` và máy không có ELF. Địa chỉ cần nằm trong vùng `0x20000000`–`0x3FFFFFFF` và căn theo kiểu dữ liệu. |
| So khớp ELF | **So khớp ELF** đọc các đoạn có dữ liệu trong flash nội và báo byte đầu tiên bị lệch. App cũng kiểm tra trước lần ghi biến theo tên đầu tiên ở mỗi kết nối hoặc sau khi mở ELF. Nếu vừa nạp firmware từ IDE hoặc công cụ khác, mở lại ELF của bản vừa nạp và chạy **So khớp ELF**. |
| Đồ thị và CSV | Mở **Đồ thị / CSV** từ Watch, chọn biến và khoảng thời gian. App thu mẫu khi cửa sổ đang mở, giữ tối đa 12.000 mẫu gần nhất và có nút dừng mẫu hoặc lưu CSV. Tốc độ lấy mẫu tùy đường truyền và số biến. |
| Profile và tuning | **Lưu profile mới** lưu cách kết nối, dòng STM32, ELF và danh sách Watch. **Lưu tuning** lấy các giá trị RAM đã đọc của những biến được chọn. **Áp tuning** ghi các giá trị vào RAM sau khi xác nhận. Kiểm tra thông báo đọc lại nếu firmware cũng cập nhật các biến đó. |
| RTT | **Log RTT** đọc log của chương trình STM32 đã tích hợp và khởi tạo SEGGER RTT. App lấy địa chỉ `_SEGGER_RTT` từ ELF khi có. Có thể nhập địa chỉ thủ công và chọn kênh, thường là kênh 0. [Tài liệu OpenOCD RTT](https://openocd.org/doc/html/General-Commands.html#Real-Time-Transfer-_0028RTT_0029) |
| Nạp và điều khiển | **Nạp ELF** ghi flash, verify và reset chạy sau khi xác nhận. **Dừng chip** halt CPU qua SWD và giữ kết nối để Watch hoặc ghi biến. **Chạy tiếp** resume từ vị trí đang dừng. |

Watch, ghi RAM và đối chiếu flash của firmware đang chạy đều có thể dùng ngay sau khi attach. Giá trị ghi vào RAM có thể bị firmware cập nhật lại hoặc mất khi reset. Profile và preset nằm trong `%LOCALAPPDATA%\STM32 Wireless Debug\settings.json` (trên Windows) hoặc `~/.config/STM32 Wireless Debug/settings.json` (trên Linux).

## Chạy từ mã nguồn

Để build app portable:
- Trên Linux (Ubuntu x64): chạy `bash host/build-linux.sh`
- Trên Windows (x64): chạy `host/build-windows.ps1` trong PowerShell với Python 64-bit. [Thông tin phần mềm kèm theo](third-party.md)

Để chạy riêng Watch, khởi động OpenOCD có hỗ trợ CMSIS-DAP TCP rồi gọi `python host/live_watch.py` trong một PowerShell khác. Cài thư viện Python từ `host/requirements.txt`. Ví dụ dùng STM32F4, thay IP dưới đây bằng IP của probe.

```powershell
$ProbeIp = '192.168.1.50'
openocd.exe -c "set PROBE_IP $ProbeIp" -c 'set TARGET_SCRIPT target/stm32f4x.cfg' -f host/wireless.cfg
```

