# Checklist triển khai probe STM32 qua Wi‑Fi

## Mốc 1 · Kết nối tới probe

- [ ] Xác nhận model STM32, revision SuperMini, chân GPIO4/5 cho SWD và đầu nối target. Xác định pad NRST nếu cần dùng dây thứ tư. Đo VTref khi board đích tự cấp nguồn. Nếu target có probe tích hợp, đối chiếu tài liệu board để chọn đầu nối và chế độ probe ngoài. Phụ thuộc: phần cứng trên bàn.
- [ ] Khóa phiên bản firmware tham chiếu và công cụ build. Tạo cấu hình SuperMini cho một cổng SWD ba dây, Wi‑Fi STA và console USB. Tắt `Target nRESET supported` trong cấu hình probe. Tắt TCP console, UART bridge và các cổng phụ trong cấu hình. Xác minh build thành công, flash qua USB, boot, vào Wi‑Fi 2,4 GHz của router và log ra IP DHCP cùng ánh xạ chân SWD. Phụ thuộc: xác nhận SuperMini.
- [ ] Dùng xPack OpenOCD đã kiểm tra trên Windows để đọc thông tin probe CMSIS-DAP qua TCP 4441. Thử kết nối bằng một OpenOCD thứ hai trong lúc phiên đầu đang giữ probe và kiểm tra probe chỉ phục vụ một phiên. Xác nhận PC vẫn truy cập Internet trong khi OpenOCD đang kết nối. Phụ thuộc vào firmware ESP đang chạy và LAN cho phép hai thiết bị liên lạc.

## Mốc 2 · Verify flash và debug STM32

- [ ] Nối SWD theo đúng pinout và thử ở 100 kHz. OpenOCD đọc được DPIDR, nhận core đích và halt. Dùng `reset_config none` rồi kiểm tra software reset/halt qua SWD. Nếu lắp dây NRST, đo xung reset và kiểm tra reset cứng trước khi thử connect-under-reset. Phụ thuộc: mốc 1.
- [ ] Tạo ELF thử có LED nháy, symbol debug và một biến quan sát được. Chạy `program firmware.elf verify reset exit` trên đúng cấu hình target. Tiêu chí đạt là OpenOCD thoát với mã 0, log báo verify thành công và LED chạy đúng sau reset. Phụ thuộc: nhận và reset target.
- [ ] Dùng GDB trên PC attach tới `127.0.0.1:3333`. Reset/halt trước khi đặt breakpoint `main`, sau đó continue tới breakpoint, step qua mã nguồn và đọc biến. Lặp lại qua cấu hình external GDB server của IDE. Tách thao tác nạp và attach để chỉ có một tiến trình OpenOCD giữ probe tại mỗi thời điểm. Phụ thuộc: ELF đã nạp và symbol khớp.

## Mốc 3 · Kiểm tra kết nối khi đi hiện trường

- [ ] Thử Windows Mobile hotspot ở băng 2,4 GHz với ESP ở chế độ STA. Kiểm tra PC giữ Internet và OpenOCD vẫn nạp/verify, halt và step được. Phụ thuộc vào mốc 2 và PC có thể bật hotspot.
- [ ] Làm bản thử BLE GATT sau khi luồng SWD qua TCP chạy ổn. Kiểm tra PC ghép cặp với probe, OpenOCD nạp/verify và GDB step được trong lúc PC dùng Wi‑Fi hiện tại. Ghi thời gian nạp, độ trễ step và khả năng kết nối lại để quyết định đưa BLE vào bản đi hiện trường. Phụ thuộc vào mốc 2 và PC có Bluetooth LE.

## Mốc 4 · Chạy ổn định và đóng gói cách sử dụng

- [ ] Chạy 10 vòng nạp/verify/reset với nguồn rời cho ESP và STM32. Ghi tốc độ SWD, số byte image thực nạp và thời gian từng vòng. Chọn tốc độ cao nhất đã thử mà cả 10 vòng đều qua. Phụ thuộc: mốc 2.
- [ ] Ngắt Wi‑Fi lúc ghi flash trên board thử và lúc CPU đang halt. Xác minh lỗi được báo khi đang nạp, SWD về input khi đóng phiên lỗi, mở phiên mới được sau khi Wi‑Fi trở lại và nạp lại verify thành công. Nếu lắp dây NRST, thử ngắt Wi‑Fi lúc probe đang giữ reset và xác nhận NRST được nhả. Ghi thao tác reset/resume nếu core đang halt sau khi mất kết nối. Phụ thuộc: mốc 2.
- [ ] Lưu cấu hình board, lệnh nạp và lệnh bật GDB server cho Windows, sơ đồ dây và log nghiệm thu. Kiểm tra lại từ lúc cấp nguồn USB rời tới lần nạp và debug thành công. Phụ thuộc vào hai phép thử độ ổn định của mốc 4.
