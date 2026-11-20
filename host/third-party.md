# Third-party software

The Windows app launches the unmodified xPack OpenOCD Windows x64 distribution v0.12.0-7 as a separate process. Its distribution README and component license files are included under openocd/distro-info and openocd/README.md in the portable application bundle. OpenOCD is licensed under GPLv2. The separately included vendor DLLs retain their own notices.

- [xPack Windows release and archive checksums](https://github.com/xpack-dev-tools/openocd-xpack/releases/tag/v0.12.0-7)
- [xPack source, changes and build scripts](https://github.com/xpack-dev-tools/openocd-xpack/tree/v0.12.0-7)
- [Pinned upstream OpenOCD source](https://github.com/openocd-org/openocd/tree/e5888bda38f4952e2ae92f7dc5b25fc9a2d1c2b3)

The app also uses [Bleak](https://github.com/hbldh/bleak), [PyWinRT](https://github.com/pywinrt/pywinrt), [pySerial](https://github.com/pyserial/pyserial), [pyelftools](https://github.com/eliben/pyelftools), and the [PyInstaller bootloader](https://github.com/pyinstaller/pyinstaller).
