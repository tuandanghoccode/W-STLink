"""Plot and export samples already read by Live Watch."""

from collections import Counter, deque
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HISTORY_LIMIT = 12_000
COLORS = ("#1756A9", "#A23754", "#16775E", "#8C5214", "#6A4796", "#00748A")


@dataclass(frozen=True)
class Sample:
    elapsed: float
    timestamp: datetime
    values: dict


class PlotWindow(tk.Toplevel):
    """A time series window that receives numeric snapshots on the Tk thread."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Đồ thị Live Watch")
        self.geometry("860x490")
        self.minsize(650, 330)
        self._history = deque(maxlen=HISTORY_LIMIT)
        self._columns = {}
        self._active = {}
        self._paused = None
        self._draw_id = None

        controls = ttk.Frame(self, padding=(12, 12, 12, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="Khoảng hiển thị").pack(side="left")
        self._seconds = tk.StringVar(value="30")
        period = ttk.Combobox(
            controls, state="readonly", width=5, textvariable=self._seconds,
            values=("10", "30", "60", "120"),
        )
        period.pack(side="left", padx=(8, 4))
        period.bind("<<ComboboxSelected>>", self._schedule_draw)
        ttk.Label(controls, text="giây").pack(side="left")
        ttk.Button(controls, text="Xuất CSV…", command=self._save_csv).pack(side="right")
        self._pause_button = ttk.Button(controls, text="Tạm dừng", command=self._toggle_pause)
        self._pause_button.pack(side="right", padx=8)

        content = ttk.Frame(self, padding=(12, 0, 12, 0))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self._canvas = tk.Canvas(
            content, bg="#FFFFFF", highlightthickness=1, highlightbackground="#D5DEE9"
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas.bind("<Configure>", self._schedule_draw)

        sidebar = ttk.LabelFrame(content, text="Biến hiển thị", padding=8)
        sidebar.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        self._legend = tk.Listbox(sidebar, selectmode="multiple", exportselection=False, width=22)
        self._legend.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(sidebar, orient="vertical", command=self._legend.yview)
        scrollbar.pack(side="left", fill="y")
        self._legend.configure(yscrollcommand=scrollbar.set)
        self._legend.bind("<<ListboxSelect>>", self._schedule_draw)

        self._status = ttk.Label(self, padding=(12, 8))
        self._status.pack(fill="x")
        self._set_status()

    def sync_series(self, mapping):
        """Set the currently watched row IDs and display labels."""
        if mapping == self._active:
            return
        old_ids = list(self._active)
        selected = {old_ids[index] for index in self._legend.curselection()}
        self._active = dict(mapping)
        self._columns.update(mapping)

        self._legend.delete(0, "end")
        for index, (key, label) in enumerate(mapping.items()):
            self._legend.insert("end", label)
            self._legend.itemconfigure(index, foreground=self._color(key))
            if key in selected or key not in old_ids:
                self._legend.selection_set(index)
        self._schedule_draw()

    def add_sample(self, values):
        """Append a snapshot from an existing poll without another target read."""
        recorded = {
            key: value for key, value in values.items()
            if key in self._active and isinstance(value, (int, float)) and math.isfinite(value)
        }
        if not recorded:
            return

        self._history.append(Sample(monotonic(), datetime.now(timezone.utc), recorded))
        self._set_status()
        if self._paused is None:
            self._schedule_draw()

    def _color(self, key):
        return COLORS[list(self._columns).index(key) % len(COLORS)]

    def _set_status(self):
        prefix = "Đồ thị tạm dừng, vẫn ghi mẫu" if self._paused is not None else "Đang theo dõi"
        self._status.configure(text=f"{prefix}   ·   {len(self._history):,} mẫu đã lưu")

    def _toggle_pause(self):
        self._paused = tuple(self._history) if self._paused is None else None
        self._pause_button.configure(text="Tiếp tục" if self._paused is not None else "Tạm dừng")
        self._set_status()
        self._schedule_draw()

    def _schedule_draw(self, _event=None):
        if self._draw_id is None:
            self._draw_id = self.after_idle(self._draw)

    def destroy(self):
        if self._draw_id is not None:
            self.after_cancel(self._draw_id)
            self._draw_id = None
        super().destroy()

    def _draw(self):
        self._draw_id = None
        canvas = self._canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        left, top, right, bottom = 65, 18, width - 34, height - 38
        if right <= left or bottom <= top:
            return

        active_ids = list(self._active)
        selected = [active_ids[index] for index in self._legend.curselection()]
        source = self._history if self._paused is None else self._paused
        if not source or not selected:
            self._empty_chart(width, height, "Chọn biến để xem đồ thị." if source else "Đang chờ dữ liệu Live Watch.")
            return

        duration = int(self._seconds.get())
        last = source[-1]
        earliest = last.elapsed - duration
        samples = [sample for sample in source if sample.elapsed >= earliest]
        values = [sample.values[key] for sample in samples for key in selected if key in sample.values]
        if not values:
            self._empty_chart(width, height, "Chưa có mẫu cho các biến đã chọn.")
            return

        low, high = min(values), max(values)
        padding = (high - low) * .05 or max(abs(low) * .05, 1)
        low, high = low - padding, high + padding
        grid, ink = "#E2E9F1", "#43566A"

        for step in range(5):
            fraction = step / 4
            y = bottom - fraction * (bottom - top)
            canvas.create_line(left, y, right, y, fill=grid)
            canvas.create_text(left - 8, y, text=f"{low + fraction * (high - low):.4g}",
                               fill=ink, anchor="e")
            x = left + fraction * (right - left)
            stamp = last.timestamp - timedelta(seconds=duration * (1 - fraction))
            canvas.create_line(x, top, x, bottom, fill=grid)
            canvas.create_text(x, bottom + 10, text=stamp.astimezone().strftime("%H:%M:%S"),
                               fill=ink, anchor="n")

        canvas.create_line(left, top, left, bottom, right, bottom, fill="#8797A6")
        for key in selected:
            points = []
            for sample in samples:
                if key not in sample.values:
                    self._draw_series(points, self._color(key))
                    points = []
                    continue
                x = left + (sample.elapsed - earliest) / duration * (right - left)
                y = bottom - (sample.values[key] - low) / (high - low) * (bottom - top)
                points.extend((x, y))
            self._draw_series(points, self._color(key))

    def _draw_series(self, points, color):
        if len(points) >= 4:
            self._canvas.create_line(*points, fill=color, width=2, tags="series")
        elif points:
            x, y = points
            self._canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="", tags="series")

    def _empty_chart(self, width, height, message):
        self._canvas.create_text(width / 2, height / 2, text=message, fill="#43566A")

    def _save_csv(self):
        if not self._history:
            self._status.configure(text="Chưa có mẫu để xuất CSV.")
            return
        destination = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
        )
        if not destination:
            return
        try:
            self._write_csv(Path(destination))
        except OSError as error:
            messagebox.showerror("Không xuất được CSV", str(error), parent=self)
        else:
            self._status.configure(text=f"Đã xuất {len(self._history):,} mẫu vào {Path(destination).name}")

    def _write_csv(self, destination):
        counts = Counter(self._columns.values())
        columns = list(self._columns)
        headings = [
            label if counts[label] == 1 else f"{label} [{key}]"
            for key, label in self._columns.items()
        ]
        headings = ["'" + label if label.startswith(("=", "+", "-", "@")) else label for label in headings]
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8-sig",
                prefix=".live-watch-", suffix=".csv", dir=destination.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(["timestamp", *headings])
                for sample in self._history:
                    writer.writerow([
                        sample.timestamp.isoformat(timespec="milliseconds"),
                        *(sample.values.get(key, "") for key in columns),
                    ])
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

