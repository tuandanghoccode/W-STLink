"""Read-only RTT console in a separate Tk window."""

from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class RttWindow(tk.Toplevel):
    def __init__(self, parent, capture, on_close):
        super().__init__(parent)
        self.title("STM32 · Log RTT")
        self.geometry("700x410")
        self.capture = capture
        self.on_close = on_close
        self.stop_event = Event()
        self.events = Queue(maxsize=128)
        self.tick = None

        toolbar = ttk.Frame(self, padding=8)
        ttk.Button(toolbar, text="Lưu log", command=self.save).pack(side="left")
        ttk.Button(toolbar, text="Xóa nội dung", command=self.clear).pack(side="left", padx=8)
        self.status = tk.StringVar(value="Đang đọc RTT")
        ttk.Label(toolbar, textvariable=self.status).pack(side="right")
        toolbar.pack(fill="x")

        body = ttk.Frame(self, padding=(8, 0, 8, 8))
        self.output = tk.Text(body, wrap="none", state="disabled", font="TkFixedFont")
        scroll = ttk.Scrollbar(body, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.stop)
        Thread(target=self.reader, daemon=True).start()
        self.tick = self.after(100, self.render)

    def reader(self):
        try:
            while not self.stop_event.is_set():
                text = self.capture.read()
                if text:
                    try:
                        self.events.put(text, timeout=0.3)
                    except Full:
                        if not self.stop_event.is_set():
                            raise RuntimeError("Bộ đệm log RTT đã đầy.")
        except (RuntimeError, OSError) as error:
            if not self.stop_event.is_set():
                try:
                    self.events.put(str(error), timeout=0.3)
                except Full:
                    pass
        finally:
            self.stop_event.set()

    def render(self):
        messages = []
        while True:
            try:
                messages.append(self.events.get_nowait())
            except Empty:
                break
        if messages:
            self.output.configure(state="normal")
            self.output.insert("end", "".join(messages))
            lines = int(self.output.index("end-1c").split(".")[0])
            if lines > 4000:
                self.output.delete("1.0", f"{lines - 3999}.0")
            self.output.see("end")
            self.output.configure(state="disabled")
        if self.stop_event.is_set():
            self.status.set("Đã dừng đọc")
        self.tick = self.after(100, self.render)

    def clear(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def save(self):
        filename = filedialog.asksaveasfilename(parent=self, defaultextension=".txt")
        if filename:
            try:
                Path(filename).write_text(self.output.get("1.0", "end-1c"), encoding="utf-8")
            except OSError as error:
                messagebox.showerror("Lỗi lưu log", str(error), parent=self)

    def stop(self):
        self.stop_event.set()
        if self.tick is not None:
            self.after_cancel(self.tick)
        self.destroy()
        self.on_close(self.capture)
