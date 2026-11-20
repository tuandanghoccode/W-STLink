# Probe STM32 qua Wi-Fi hoặc BLE cho ESP32-S3 SuperMini

Firmware biến SuperMini thành probe CMSIS-DAP. Ở chế độ Wi-Fi mặc định, ESP32 vào mạng 2,4 GHz ở chế độ Station và nhận kết nối OpenOCD qua TCP 4441. App dò probe trên cùng LAN qua UDP 4443. Ở chế độ BLE, app Windows ghép cặp với ESP32 rồi làm cầu nối cục bộ cho OpenOCD. Dùng một phiên OpenOCD mỗi lần và chỉ mở debug Wi-Fi trên LAN riêng tin cậy.

Firmware dùng mã nguồn [cmsis_dap_tcp_esp32](https://github.com/bkuschak/cmsis_dap_tcp_esp32/tree/a5b06525c8850fec3c80e379280e3b53f0789735) tại commit `a5b06525c8850fec3c80e379280e3b53f0789735`. Xem [giấy phép Apache 2.0](LICENSE) và thông báo bản quyền trong mã nguồn. Cấu hình [SuperMini](sdkconfig.defaults) dùng ESP-IDF 6.0.2, flash 4 MB, DIO 40 MHz, console USB Serial/JTAG và SWD ba dây.

## Nạp firmware vào SuperMini

Mở ESP-IDF PowerShell 6.0.2, xác định cổng COM của SuperMini và thay `COMx` bên dưới.

```powershell
Set-Location 'D:\stm32_wdebug_tool\firmware'
idf.py -D SDKCONFIG=build-ble/sdkconfig -B build-ble -p COMx flash
```

Firmware 1.2 dùng phân vùng app lớn hơn bản Wi-Fi cũ. Lệnh trên ghi bootloader, bảng phân vùng và app ở ba vùng riêng. Vị trí NVS giữ nguyên để giữ thông tin Wi-Fi đã lưu. Các địa chỉ và file đã build nằm trong [flash_args](build-ble/flash_args). [Tài liệu flash ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/v6.0.2/esp32s3/get-started/index.html)

## Đổi chế độ kết nối

Trong app Windows, chọn chế độ mong muốn và bấm **Đổi chế độ ESP** khi ESP đang cắm USB. Qua USB console có thể chạy `link ble` hoặc `link wifi`, sau đó `reboot`. Chế độ được lưu qua lần tắt nguồn. Khi dùng BLE, máy tính ghép cặp trực tiếp với probe `STM32-WDEBUG-...`.

## Cho ESP32 vào Wi-Fi

Ở prompt `esp32>` trong USB monitor, nhập hai lệnh dưới đây với SSID và mật khẩu thật. Firmware lưu thông tin qua NVS trên thiết bị. Lấy địa chỉ probe từ dòng `IP address:` sau khi khởi động lại. Windows Mobile hotspot ở băng 2,4 GHz cũng có thể làm mạng cho probe.

```text
wifi "SSID_2G4" "MAT_KHAU" wpa2
reboot
```

## Đấu STM32 và chạy OpenOCD

| ESP32-S3 | STM32 3,3 V |
| --- | --- |
| GPIO4 | SWDIO |
| GPIO5 | SWCLK |
| GND | GND |

Cấp nguồn riêng cho cả hai board và đấu dây khi đã tắt nguồn. Với ba dây, dùng `reset_config none` và đích STM32 còn truy cập được qua SWD. Board có ST-LINK tích hợp cần thiết lập chế độ probe ngoài theo tài liệu của board.

Dùng xPack OpenOCD Windows x64 [v0.12.0-7](https://github.com/xpack-dev-tools/openocd-xpack/releases/tag/v0.12.0-7) có backend CMSIS-DAP TCP. Thay IP ví dụ bằng IP của ESP và thay `target/stm32g4x.cfg` nếu dùng dòng chip khác. Chạy ở thư mục chứa `firmware.elf`.

```powershell
$ProbeIp = '192.168.1.50'
$ProbeOptions = @(
    '-c', 'adapter driver cmsis-dap'
    '-c', 'cmsis-dap backend tcp'
    '-c', "cmsis-dap tcp host $ProbeIp"
    '-c', 'cmsis-dap tcp port 4441'
    '-c', 'transport select swd'
    '-f', 'target/stm32g4x.cfg'
    '-c', 'adapter speed 100'
    '-c', 'reset_config none'
    '-c', 'bindto 127.0.0.1'
)
Test-NetConnection $ProbeIp -Port 4441
openocd.exe @ProbeOptions -c 'program firmware.elf verify reset exit'
```

Để debug, chạy `openocd.exe @ProbeOptions` và mở GDB với cùng ELF. Trong GDB, nhập `target extended-remote localhost:3333`, `monitor reset halt`, `break main` và `continue`. [OpenOCD Flash Programming](https://openocd.org/doc/html/Flash-Programming.html) · [OpenOCD GDB](https://openocd.org/doc/html/GDB-and-OpenOCD.html)

Xem thêm [bảng theo dõi và ghi biến STM32 khi đang chạy](../host/README.md).
