# Kế hoạch làm probe nạp và debug STM32 qua Wi‑Fi

Dùng ESP32-S3 SuperMini làm probe SWD đặt cạnh STM32. Bản đầu nạp flash nội từ `.elf`, verify sau khi ghi và debug ở mức mã nguồn với GDB. Mốc thử đầu tiên dùng PC Windows, một STM32 logic 3,3 V có SWD mở và dây SWD ngắn. Dùng board đích sẵn có nếu OpenOCD có cấu hình đúng dòng chip. `NUCLEO-G431RB` là một board tham chiếu nếu cần mua thêm. [Cấu hình STM32G4 của OpenOCD](https://github.com/openocd-org/openocd/blob/master/tcl/target/stm32g4x.cfg) · [Hướng dẫn Nucleo của ST](https://www.st.com/resource/en/user_manual/um2505-stm32g4-nucleo64-boards-mb1367-stmicroelectronics.pdf)

## Kiến trúc

```text
PC Windows                                  Cạnh board đích
GDB / IDE ── 127.0.0.1:3333 ── OpenOCD ── Wi-Fi TCP:4441 ── ESP32-S3 ── SWD ── STM32
```

OpenOCD trên PC dùng cấu hình theo dòng STM32 để nạp flash và làm GDB server. ESP32-S3 nhận gói CMSIS-DAP qua TCP và thực thi giao dịch SWD tại chỗ. Dùng firmware `bkuschak/cmsis_dap_tcp_esp32` tại commit `a5b06525c8850fec3c80e379280e3b53f0789735` làm nền và tạo cấu hình GPIO cho SuperMini. Upstream ghi nhận đã thử ESP32-S3 với các họ STM32 F4, F103 và G0, và dùng ESP-IDF 6.0.2. [Firmware tham chiếu](https://github.com/bkuschak/cmsis_dap_tcp_esp32/tree/a5b06525c8850fec3c80e379280e3b53f0789735) · [OpenOCD CMSIS-DAP TCP](https://www.openocd.org/doc/html/Debug-Adapter-Configuration.html)

Dùng xPack OpenOCD `v0.12.0-7` Windows x64. Đã chạy binary `0.12.0+dev-02228-ge5888bda3-dirty` trên Windows và xác nhận nó nhận backend TCP, nạp được `target/stm32g4x.cfg` và nhận cấu hình SWD/reset. Phép thử kết thúc ngay ở bước đọc cấu hình. Kết nối STM32 và tích hợp IDE được kiểm tra ở các mốc phần cứng. [Bản phát hành xPack](https://github.com/xpack-dev-tools/openocd-xpack/releases/tag/v0.12.0-7) · [GDB và OpenOCD](https://openocd.org/doc/html/GDB-and-OpenOCD.html) · [STM32CubeIDE UM2609, mục 3.1](https://www.st.com/resource/en/user_manual/dm00629856-stm32cubeide-user-guide-stmicroelectronics.pdf)

### Kết nối qua router

Mặc định dùng Wi‑Fi Station (STA). ESP32 vào Wi‑Fi 2,4 GHz của router và nhận IP qua DHCP. PC giữ Wi‑Fi 5 GHz, 2,4 GHz hoặc Ethernet đang dùng, miễn PC và ESP ở cùng LAN và router cho hai thiết bị liên lạc. Dùng mạng riêng tin cậy vì CMSIS-DAP TCP dựa vào mạng để giới hạn truy cập. Cấu hình SSID và mật khẩu qua USB serial. Đọc IP ở log ESP hoặc danh sách DHCP trên router. Có thể đặt DHCP reservation trên router để giữ IP ổn định. [Chế độ STA của firmware](https://github.com/bkuschak/cmsis_dap_tcp_esp32#station-mode-default) · [ESP32-S3 Datasheet](https://documentation.espressif.com/esp32_s3_datasheet_en.pdf)

Ví dụ PowerShell dưới đây dùng `192.168.1.50` làm IP minh họa. Thay bằng IP của ESP rồi chạy `Test-NetConnection $ProbeIp -Port 4441` sau khi probe khởi động. Nếu cổng chưa tới được, kiểm tra IP, SSID guest và chế độ cô lập thiết bị trên router. Lệnh OpenOCD bên dưới chỉ kiểm tra cấu hình cục bộ. [Test-NetConnection](https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection)

```powershell
$ProbeIp = '192.168.1.50'
openocd.exe -c 'adapter driver cmsis-dap' -c 'cmsis-dap backend tcp' `
  -c "cmsis-dap tcp host $ProbeIp" -c 'cmsis-dap tcp port 4441' `
  -c 'transport select swd' -f target/stm32g4x.cfg -c 'adapter speed 100' `
  -c 'reset_config none' -c 'bindto 127.0.0.1' -c shutdown
```

Khi chạy với phần cứng, bỏ `-c shutdown` để mở GDB server hoặc đổi thành `-c 'program firmware.elf verify reset exit'` để nạp. Chọn `target/*.cfg` theo dòng chip. Đặt tốc độ sau `-f target/...` và kiểm tra các sự kiện reset trong script target vì chúng có thể đổi tốc độ trong lúc nạp. [Lệnh nạp OpenOCD](https://openocd.org/doc/html/Flash-Programming.html)

### Kết nối khi đi hiện trường

- **Windows Mobile hotspot.** PC giữ đường Internet hiện tại và phát một Wi‑Fi riêng cho ESP ở chế độ STA. Chọn băng 2,4 GHz nếu Windows cho chọn. Đọc IP mới trên log ESP, rồi kiểm tra TCP 4441 từ PC. Cách này phụ thuộc adapter và quyền bật hotspot trên máy. [Hướng dẫn Mobile hotspot của Microsoft](https://support.microsoft.com/en-us/windows/experience/connectivity-networking/use-your-windows-device-as-a-mobile-hotspot)
- **Bluetooth LE.** Dùng khi cần kết nối trực tiếp mà PC vẫn dùng Wi‑Fi hiện tại, kể cả nơi Wi‑Fi khách cô lập thiết bị hoặc máy cấm bật hotspot. ESP32-S3 hỗ trợ BLE, còn cổng COM Bluetooth kiểu SPP cần Bluetooth Classic. Với BLE, thiết kế một dịch vụ GATT trên ESP và một tiến trình cầu nối trên PC. OpenOCD kết nối TCP vào `127.0.0.1:4441` của tiến trình này, còn tiến trình chuyển gói CMSIS-DAP qua BLE tới cùng firmware SWD. Kiểm tra ghép cặp, phân mảnh gói theo MTU, timeout và khả năng kết nối lại. So sánh thời gian flash/verify và độ trễ step với STA trên cùng ELF trước khi chọn chế độ dùng chính khi đi hiện trường. [Bluetooth trên ESP32-S3](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/bt-architecture/overview.html) · [GATT client trên Windows](https://learn.microsoft.com/en-us/windows/uwp/devices-sensors/gatt-client)

## Phần cứng và đấu nối

| Món | Số lượng | Dùng cho |
| --- | --- | --- |
| ESP32-S3 SuperMini | 1, đã có | Probe Wi‑Fi và SWD |
| Cáp USB-C có data và nguồn USB 5 V | 1 bộ | Nạp firmware ESP từ PC lúc cài đặt, sau đó cấp nguồn bằng củ sạc hoặc pin dự phòng |
| Dây jumper ngắn, khoảng 10–15 cm | 3 sợi | SWDIO, SWCLK và GND. Thêm dây khi cần dùng NRST hoặc đưa VTref ra điểm đo |
| Điện trở 33 Ω | 2, tùy chọn | Thử nối tiếp SWCLK và SWDIO gần ESP nếu dây bị ringing |
| Board STM32 3,3 V và nguồn của board | 1 bộ | Dùng board đang có. Chọn cáp chuyển đúng chuẩn đầu SWD nếu board dùng đầu 1,27 mm |

| ESP32-S3 SuperMini | STM32 | Ghi chú |
| --- | --- | --- |
| GND | GND | Mass chung |
| GPIO4 | SWDIO | Hai chiều, firmware đổi hướng khi STM32 trả lời |
| GPIO5 | SWCLK | Bắt đầu ở 100 kHz, sau đó đo ở 500 kHz và 1 MHz nếu ổn định |
| GPIO6, tùy chọn | NRST | Nối khi cần reset cứng và connect-under-reset |
| Điểm đo để hở phía probe | VTref / VDD target | Dùng đồng hồ xác nhận mức logic khoảng 3,3 V trước khi đấu |

GPIO4/5 là ánh xạ SWD đề xuất, GPIO6 dành cho NRST nếu lắp thêm. Đối chiếu silkscreen hoặc sơ đồ đúng revision SuperMini trước khi đấu. GPIO19/20 dành cho USB và GPIO0/3/45/46 là chân strapping. Cấp nguồn riêng cho mỗi board, chỉ nối GND giữa hai hệ nguồn. Tắt cả hai nguồn khi cắm hoặc tháo dây debug, cấp nguồn cho cả hai trước khi chạy SWD. Nếu dùng NUCLEO-G431RB (MB1367), UM2505 mục 7.3.3 chỉ cách dùng JP1 để đưa ST-LINK tích hợp về high-Z, sau đó nối probe ngoài vào CN4 theo bảng pin của ST. [Espressif ESP32-S3 Datasheet](https://documentation.espressif.com/esp32_s3_datasheet_en.pdf) · [Firmware điều khiển NRST](https://github.com/bkuschak/cmsis_dap_tcp_esp32/blob/a5b06525c8850fec3c80e379280e3b53f0789735/main/DAP_config.h#L695-L718) · [ST UM2505, mục 7.3.3](https://www.st.com/resource/en/user_manual/um2505-stm32g4-nucleo64-boards-mb1367-stmicroelectronics.pdf)

### Reset khi dùng ba dây SWD

Tắt `Target nRESET supported` (`CONFIG_ESP_DAP_1_NRESET_SUPPORTED`) ở cấu hình firmware probe và đặt `reset_config none` sau script target trong OpenOCD. Khi SWD truy cập được, OpenOCD có thể reset qua debug port. Cấu hình `stm32g4x.cfg` của xPack chọn `SYSRESETREQ` cho trường hợp này. [Cấu hình firmware](https://github.com/bkuschak/cmsis_dap_tcp_esp32/blob/a5b06525c8850fec3c80e379280e3b53f0789735/main/Kconfig.projbuild#L30-L34) · [OpenOCD Cortex-M reset](https://openocd.org/doc/html/Architecture-and-Core-Commands.html)

Giữ pad NRST trên board đích để gắn dây thứ tư khi cần cứu firmware vô hiệu hóa SWD ngay sau boot. Khi dùng dây thứ tư, bật lại nRESET trên probe, kiểm tra reset cứng và cấu hình `reset_config srst_only srst_nogate connect_assert_srst` với target hỗ trợ. [OpenOCD Reset Configuration](https://openocd.org/doc/html/Reset-Configuration.html) · [STM32CubeProgrammer UM2237](https://www.st.com/resource/en/user_manual/dm00403500-stm32cubeprogrammer-software-description-stmicroelectronics.pdf)

## Thứ tự triển khai

1. **Kết nối.** Build firmware từ upstream cho đúng SuperMini. Dùng STA trên LAN riêng tin cậy và lấy IP từ log USB. Tắt `ESP_DAP_SOCKET_CONSOLE_ENABLED`, `ESP_UART_BRIDGE_1_ENABLED` và các dịch vụ phụ, giữ console USB. Đọc thông tin CMSIS-DAP trước khi nối STM32 và xác nhận PC vẫn truy cập Internet. [Cấu hình STA của firmware](https://github.com/bkuschak/cmsis_dap_tcp_esp32#station-mode-default)
2. **Nạp code.** Nối một STM32 đã xác định model và mức I/O. OpenOCD phải nhận được SWD DPIDR và core, sau đó nạp một ELF nháy LED, verify và reset chạy. Thử với ba dây và xác nhận software reset chạy đúng. [OpenOCD Flash Programming](https://openocd.org/doc/html/Flash-Programming.html) · [OpenOCD Reset Configuration](https://openocd.org/doc/html/Reset-Configuration.html)
3. **Debug mã nguồn.** Chạy OpenOCD nền trên PC, kết nối `arm-none-eabi-gdb` tới `127.0.0.1:3333` với cùng ELF có symbol. Chạy `monitor reset halt`, đặt breakpoint tại `main`, `continue` tới breakpoint rồi step và đọc biến. Sau đó dùng chế độ kết nối GDB server có sẵn trong STM32CubeIDE với cùng địa chỉ. [OpenOCD GDB](https://openocd.org/doc/html/GDB-and-OpenOCD.html) · [STM32CubeIDE UM2609](https://www.st.com/resource/en/user_manual/dm00629856-stm32cubeide-user-guide-stmicroelectronics.pdf)
4. **Kiểm tra độ ổn định.** Nạp và verify 10 lần liên tiếp, ghi số byte thực nạp, thời gian nạp và tốc độ SWD. Khi rớt Wi‑Fi trong lúc nạp, PC phải báo lỗi. Khi đóng phiên lỗi, probe cần đưa SWD về input để phiên mới có thể nạp lại từ đầu và verify. Nếu có lắp NRST, kiểm tra thêm mất mạng khi đang giữ reset và xác nhận probe nhả NRST. Đường đóng socket của firmware tham chiếu cần kiểm tra thêm ở điểm này. Nếu core đang halt, kiểm tra trạng thái và chủ động reset hoặc resume sau khi kết nối lại. Lặp lại với nguồn USB rời cho probe. [Mã máy chủ TCP của firmware](https://github.com/bkuschak/cmsis_dap_tcp_esp32/blob/a5b06525c8850fec3c80e379280e3b53f0789735/main/cmsis_dap_tcp.c#L600-L668)

Sau khi đạt các mốc trên, phần cứng có thể gọn lại thành carrier với đầu Cortex Debug 10 pin hoặc cáp theo target. Nếu cần target 1,8 V hoặc hai board có thể bật/tắt độc lập trong khi vẫn cắm dây, thiết kế tầng đệm có chế độ high‑Z khi mất nguồn một phía. Với SWDIO, tầng đệm cần đổi chiều đúng pha turnaround của SWD. UART log qua Wi‑Fi có thể thêm riêng khi cần. [Cortex Debug Connector của Arm](https://documentation-service.arm.com/static/5fce6c49e167456a35b36af1) · [Ví dụ IC dịch mức có VCC isolation của TI](https://www.ti.com/product/SN74AXC1T45)

[Checklist triển khai và phép thử](todo.md)
