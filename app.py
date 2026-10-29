from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageTk


APP_VERSION = "1.2.2"
APP_TITLE = f"手動流速測定 v{APP_VERSION}"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
AUTOSAVE_SCHEMA_VERSION = 1


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", path.name)]


def list_images(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.casefold() in IMAGE_EXTENSIONS),
        key=natural_key,
    )


def adjust_image_for_display(
    image: Image.Image,
    brightness: float,
    contrast: float,
) -> Image.Image:
    """Return a display-only adjusted copy without modifying the source image."""
    adjusted = image.copy()
    if not math.isclose(brightness, 1.0):
        adjusted = ImageEnhance.Brightness(adjusted).enhance(brightness)
    if not math.isclose(contrast, 1.0):
        adjusted = ImageEnhance.Contrast(adjusted).enhance(contrast)
    return adjusted


def default_autosave_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ManualFlowVelocity" / "autosave"
    return Path.home() / ".manual_flow_velocity" / "autosave"


def error_log_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = (
        Path(local_app_data) / "ManualFlowVelocity"
        if local_app_data
        else Path.home() / ".manual_flow_velocity"
    )
    return base / "起動エラー.txt"


def projected_displacement(
    before: tuple[float, float],
    after: tuple[float, float],
    calibration_start: tuple[float, float],
    calibration_end: tuple[float, float],
    real_length: float,
) -> tuple[float, float]:
    """Return projected displacement in real units and calibration px length."""
    axis_x = calibration_end[0] - calibration_start[0]
    axis_y = calibration_end[1] - calibration_start[1]
    pixel_length = math.hypot(axis_x, axis_y)
    if pixel_length <= 0:
        raise ValueError("校正点の間隔が0です。")
    unit_x, unit_y = axis_x / pixel_length, axis_y / pixel_length
    move_x = after[0] - before[0]
    move_y = after[1] - before[1]
    projected_pixels = move_x * unit_x + move_y * unit_y
    return projected_pixels * real_length / pixel_length, pixel_length


def calculate_velocity(
    before: tuple[float, float],
    after: tuple[float, float],
    calibration_start: tuple[float, float],
    calibration_end: tuple[float, float],
    real_length: float,
    fps: float,
    frame_gap: int,
) -> tuple[float, float, float]:
    if fps <= 0:
        raise ValueError("フレームレートは0より大きくしてください。")
    if frame_gap <= 0:
        raise ValueError("コマ間隔は1以上にしてください。")
    displacement, calibration_pixels = projected_displacement(
        before, after, calibration_start, calibration_end, real_length
    )
    delta_time = frame_gap / fps
    return displacement, displacement / delta_time, calibration_pixels


@dataclass
class Measurement:
    number: int
    before_frame: int
    after_frame: int
    before_file: str
    after_file: str
    time_s: float
    delta_time_s: float
    displacement: float
    velocity: float
    unit: str
    before_x_px: float
    before_y_px: float
    after_x_px: float
    after_y_px: float


class ImageCanvas(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        on_click: Callable[[str, tuple[float, float]], None],
        side_name: str,
        overlay_provider: Callable[[str], list[dict]],
    ) -> None:
        super().__init__(parent)
        self.side_name = side_name
        self.on_click = on_click
        self.overlay_provider = overlay_provider
        self.title_var = tk.StringVar(value=title)
        ttk.Label(self, textvariable=self.title_var, font=("", 11, "bold")).pack(
            fill="x", padx=4, pady=(2, 4)
        )
        self.canvas = tk.Canvas(
            self, background="#20242a", highlightthickness=1,
            highlightbackground="#667085", cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)
        self.source_image: Image.Image | None = None
        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.brightness = 1.0
        self.contrast = 1.0
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._drag_start: tuple[int, int] | None = None
        self._fit_pending = False

        self.canvas.bind("<Button-1>", self._left_click)
        self.canvas.bind("<MouseWheel>", self._mouse_wheel)
        self.canvas.bind("<Button-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_move)
        self.canvas.bind("<Button-3>", self._pan_start)
        self.canvas.bind("<B3-Motion>", self._pan_move)
        self.canvas.bind("<Configure>", self._on_resize)

    def set_image(
        self,
        path: Path,
        heading: str,
        preserve_view: bool = True,
    ) -> None:
        had_image = self.source_image is not None
        if self.image is not None:
            self.image.close()
        if self.source_image is not None:
            self.source_image.close()
        with Image.open(path) as source:
            self.source_image = source.convert("RGB")
        self.image = adjust_image_for_display(
            self.source_image, self.brightness, self.contrast
        )
        self.title_var.set(heading)
        if preserve_view and had_image:
            self._fit_pending = False
            self.after_idle(self.redraw)
        else:
            self._fit_pending = True
            self.after_idle(self.fit)

    def set_adjustments(self, brightness: float, contrast: float) -> None:
        self.brightness = brightness
        self.contrast = contrast
        if self.source_image is None:
            return
        if self.image is not None:
            self.image.close()
        self.image = adjust_image_for_display(
            self.source_image, brightness, contrast
        )
        self.redraw()

    def fit(self) -> None:
        if self.image is None:
            return
        width = max(self.canvas.winfo_width(), 50)
        height = max(self.canvas.winfo_height(), 50)
        self.scale = min(width / self.image.width, height / self.image.height)
        self.offset_x = (width - self.image.width * self.scale) / 2
        self.offset_y = (height - self.image.height * self.scale) / 2
        self._fit_pending = False
        self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        if self.image is None:
            return
        canvas_width = max(self.canvas.winfo_width(), 1)
        canvas_height = max(self.canvas.winfo_height(), 1)

        left = max(0, int(math.floor(-self.offset_x / self.scale)))
        top = max(0, int(math.floor(-self.offset_y / self.scale)))
        right = min(
            self.image.width,
            int(math.ceil((canvas_width - self.offset_x) / self.scale)),
        )
        bottom = min(
            self.image.height,
            int(math.ceil((canvas_height - self.offset_y) / self.scale)),
        )
        if right > left and bottom > top:
            cropped = self.image.crop((left, top, right, bottom))
            display_width = max(1, int(round((right - left) * self.scale)))
            display_height = max(1, int(round((bottom - top) * self.scale)))
            cropped = cropped.resize(
                (display_width, display_height), Image.Resampling.LANCZOS
            )
            self.photo = ImageTk.PhotoImage(cropped)
            x = self.offset_x + left * self.scale
            y = self.offset_y + top * self.scale
            self.canvas.create_image(x, y, image=self.photo, anchor="nw")

        for overlay in self.overlay_provider(self.side_name):
            self._draw_overlay(overlay)

        zoom = self.scale * 100
        self.canvas.create_text(
            8, canvas_height - 8, anchor="sw",
            text=f"{zoom:.0f}%", fill="white",
            font=("", 9), tags="hud"
        )

    def _draw_overlay(self, overlay: dict) -> None:
        color = overlay.get("color", "#00ff88")
        kind = overlay["type"]
        if kind == "line":
            x1, y1 = self.image_to_canvas(overlay["p1"])
            x2, y2 = self.image_to_canvas(overlay["p2"])
            self.canvas.create_line(
                x1, y1, x2, y2, fill=color, width=3,
                dash=overlay.get("dash")
            )
        elif kind == "point":
            x, y = self.image_to_canvas(overlay["p"])
            radius = 7
            self.canvas.create_oval(
                x - radius, y - radius, x + radius, y + radius,
                outline="black", width=4
            )
            self.canvas.create_oval(
                x - radius, y - radius, x + radius, y + radius,
                outline=color, width=2
            )
            if overlay.get("label"):
                self.canvas.create_text(
                    x + 10, y - 10, text=overlay["label"], fill=color,
                    anchor="sw", font=("", 10, "bold")
                )
        elif kind == "cross":
            x, y = self.image_to_canvas(overlay["p"])
            radius = 11
            for width, line_color in ((5, "black"), (2, color)):
                self.canvas.create_line(
                    x - radius, y, x + radius, y,
                    fill=line_color, width=width
                )
                self.canvas.create_line(
                    x, y - radius, x, y + radius,
                    fill=line_color, width=width
                )
            self.canvas.create_oval(
                x - radius, y - radius, x + radius, y + radius,
                outline=color, width=2, dash=(4, 3)
            )
            if overlay.get("label"):
                self.canvas.create_text(
                    x + 14, y - 12, text=overlay["label"], fill=color,
                    anchor="sw", font=("", 10, "bold")
                )

    def image_to_canvas(self, point: tuple[float, float]) -> tuple[float, float]:
        return (
            self.offset_x + point[0] * self.scale,
            self.offset_y + point[1] * self.scale,
        )

    def canvas_to_image(self, x: float, y: float) -> tuple[float, float] | None:
        if self.image is None:
            return None
        image_x = (x - self.offset_x) / self.scale
        image_y = (y - self.offset_y) / self.scale
        if 0 <= image_x < self.image.width and 0 <= image_y < self.image.height:
            return image_x, image_y
        return None

    def _left_click(self, event: tk.Event) -> None:
        # Move keyboard focus away from the previously pressed navigation button.
        # Otherwise Enter can invoke that button again before recording.
        self.canvas.focus_set()
        point = self.canvas_to_image(event.x, event.y)
        if point is not None:
            self.on_click(self.side_name, point)

    def _mouse_wheel(self, event: tk.Event) -> None:
        if self.image is None:
            return
        old_scale = self.scale
        factor = 1.25 if event.delta > 0 else 0.8
        fit_scale = min(
            max(self.canvas.winfo_width(), 50) / self.image.width,
            max(self.canvas.winfo_height(), 50) / self.image.height,
        )
        self.scale = min(max(old_scale * factor, fit_scale * 0.5), 8.0)
        image_x = (event.x - self.offset_x) / old_scale
        image_y = (event.y - self.offset_y) / old_scale
        self.offset_x = event.x - image_x * self.scale
        self.offset_y = event.y - image_y * self.scale
        self.redraw()

    def _pan_start(self, event: tk.Event) -> None:
        self._drag_start = (event.x, event.y)

    def _pan_move(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        self.offset_x += event.x - self._drag_start[0]
        self.offset_y += event.y - self._drag_start[1]
        self._drag_start = (event.x, event.y)
        self.redraw()

    def _on_resize(self, _event: tk.Event) -> None:
        if self._fit_pending:
            self.fit()
        else:
            self.redraw()


class StartupDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, default_folder: Path) -> None:
        super().__init__(parent)
        self.title("初期設定")
        self.resizable(False, False)
        self.result: dict | None = None
        self.folder_var = tk.StringVar(
            value=str(default_folder) if default_folder.exists() else ""
        )
        self.fps_var = tk.DoubleVar(value=240.0)
        self.unit_var = tk.StringVar(value="cm")

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="画像フォルダ").grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.folder_var, width=56).grid(
            row=1, column=0, sticky="ew", pady=(3, 12)
        )
        ttk.Button(body, text="参照…", command=self._browse).grid(
            row=1, column=1, padx=(8, 0), pady=(3, 12)
        )
        ttk.Label(body, text="フレームレート (fps)").grid(
            row=2, column=0, sticky="w"
        )
        ttk.Spinbox(
            body, from_=0.001, to=100000, increment=1,
            textvariable=self.fps_var, width=16
        ).grid(row=3, column=0, sticky="w", pady=(3, 12))
        ttk.Label(body, text="長さの単位").grid(row=4, column=0, sticky="w")
        ttk.Combobox(
            body, textvariable=self.unit_var,
            values=("mm", "cm", "m"), state="readonly", width=12
        ).grid(row=5, column=0, sticky="w", pady=(3, 16))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="開始", command=self._accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(
            side="right", padx=(0, 8)
        )
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.transient(parent)
        self.grab_set()
        self.after_idle(self._center)
        self.after(150, self._bring_to_front)

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self, initialdir=self.folder_var.get() or None
        )
        if selected:
            self.folder_var.set(selected)

    def _accept(self) -> None:
        folder = Path(self.folder_var.get())
        try:
            fps = float(self.fps_var.get())
        except (ValueError, tk.TclError):
            fps = 0
        if not folder.is_dir():
            messagebox.showerror("初期設定", "画像フォルダを選択してください。", parent=self)
            return
        if fps <= 0:
            messagebox.showerror(
                "初期設定", "フレームレートは0より大きくしてください。", parent=self
            )
            return
        if len(list_images(folder)) < 2:
            messagebox.showerror(
                "初期設定", "画像が2枚以上あるフォルダを選択してください。", parent=self
            )
            return
        self.result = {
            "folder": folder,
            "fps": fps,
            "unit": self.unit_var.get(),
        }
        self.destroy()

    def _center(self) -> None:
        self.update_idletasks()
        parent = self.master
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _bring_to_front(self) -> None:
        self.lift()
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))
        self.focus_force()


class FlowVelocityApp:
    def __init__(
        self,
        root: tk.Tk,
        autosave_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1500x920")
        self.root.minsize(1100, 720)

        self.folder: Path | None = None
        self.images: list[Path] = []
        self.current_index = 0
        self.calibration_points: list[tuple[float, float]] = []
        self.track_before: tuple[float, float] | None = None
        self.track_after: tuple[float, float] | None = None
        self.particle_clicks: dict[int, tuple[float, float]] = {}
        self.measurements: list[Measurement] = []
        self.mode = "calibration"

        self.fps_var = tk.DoubleVar(value=240.0)
        self.unit_var = tk.StringVar(value="cm")
        self.real_length_var = tk.DoubleVar(value=10.0)
        self.frame_gap_var = tk.IntVar(value=1)
        self.show_before_position_var = tk.BooleanVar(value=True)
        self.autosave_enabled_var = tk.BooleanVar(value=True)
        self.brightness_var = tk.DoubleVar(value=1.0)
        self.contrast_var = tk.DoubleVar(value=1.0)
        self.brightness_label_var = tk.StringVar(value="1.00")
        self.contrast_label_var = tk.StringVar(value="1.00")
        self.seek_var = tk.DoubleVar(value=0.0)
        self.frame_number_var = tk.IntVar(value=1)
        self.seek_label_var = tk.StringVar(value="画像未読込")
        self.autosave_status_var = tk.StringVar(value="自動保存: 待機")
        self.status_var = tk.StringVar(value="初期設定を行ってください。")
        self.calibration_info_var = tk.StringVar(value="未校正")
        self.result_info_var = tk.StringVar(value="粒子位置を指定してください。")
        self.summary_var = tk.StringVar(value="測定数 0")
        self.selection_info_var = tk.StringVar(value="選択 0件")
        self._recalculation_job: str | None = None
        self._display_adjustment_job: str | None = None
        self._autosave_job: str | None = None
        self._seekbar_job: str | None = None
        self._updating_seekbar = False
        self._restoring_session = False
        self.autosave_dir = autosave_dir or default_autosave_directory()

        self._build_ui()
        self.real_length_var.trace_add("write", self._calculation_setting_changed)
        self.fps_var.trace_add("write", self._calculation_setting_changed)
        self.unit_var.trace_add("write", self._calculation_setting_changed)
        self.brightness_var.trace_add("write", self._display_setting_changed)
        self.contrast_var.trace_add("write", self._display_setting_changed)
        self.root.bind("<Control-o>", lambda _e: self.change_folder())
        self.root.bind("<Control-s>", lambda _e: self.export_csv())
        self.root.bind("<Control-a>", lambda _e: self.select_all_results())
        self.root.bind("<Return>", self._handle_enter)
        self.root.bind("<Left>", self._handle_left_arrow)
        self.root.bind("<Right>", self._handle_right_arrow)
        self.root.bind("<Escape>", lambda _e: self.clear_current_points())
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _handle_enter(self, _event: tk.Event) -> str:
        self.record_and_next()
        return "break"

    @staticmethod
    def _is_value_input_widget(widget: tk.Misc) -> bool:
        return widget.winfo_class() in {
            "Entry", "TEntry", "Spinbox", "TSpinbox",
            "TCombobox", "Text",
        }

    def _handle_left_arrow(self, event: tk.Event) -> str | None:
        if self._is_value_input_widget(event.widget):
            return None
        self.previous_pair()
        return "break"

    def _handle_right_arrow(self, event: tk.Event) -> str | None:
        if self._is_value_input_widget(event.widget):
            return None
        self.skip_to_next_pair()
        return "break"

    def _display_setting_changed(self, *_args) -> None:
        try:
            brightness = min(max(float(self.brightness_var.get()), 0.2), 3.0)
            contrast = min(max(float(self.contrast_var.get()), 0.2), 3.0)
        except (ValueError, tk.TclError):
            return
        self.brightness_label_var.set(f"{brightness:.2f}")
        self.contrast_label_var.set(f"{contrast:.2f}")
        if self._restoring_session:
            return
        if self._display_adjustment_job is not None:
            self.root.after_cancel(self._display_adjustment_job)
        self._display_adjustment_job = self.root.after(
            120, self._apply_display_adjustments
        )
        self._schedule_autosave()

    def _apply_display_adjustments(self) -> None:
        if self._display_adjustment_job is not None:
            try:
                self.root.after_cancel(self._display_adjustment_job)
            except tk.TclError:
                pass
        self._display_adjustment_job = None
        try:
            brightness = min(max(float(self.brightness_var.get()), 0.2), 3.0)
            contrast = min(max(float(self.contrast_var.get()), 0.2), 3.0)
        except (ValueError, tk.TclError):
            return
        self.before_canvas.set_adjustments(brightness, contrast)
        self.after_canvas.set_adjustments(brightness, contrast)

    def reset_display_adjustments(self) -> None:
        self.brightness_var.set(1.0)
        self.contrast_var.set(1.0)

    def _show_before_position_toggled(self) -> None:
        self._redraw_both()
        self._schedule_autosave()

    def _autosave_toggled(self) -> None:
        if self.autosave_enabled_var.get():
            self.autosave_status_var.set("自動保存: ON")
            self._schedule_autosave(delay_ms=0)
        else:
            if self._autosave_job is not None:
                self.root.after_cancel(self._autosave_job)
                self._autosave_job = None
            self.autosave_status_var.set("自動保存: OFF")

    def _autosave_path_for_folder(self, folder: Path) -> Path:
        normalized = str(folder.resolve()).casefold()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
        return self.autosave_dir / f"session-{digest}.json"

    def _schedule_autosave(self, delay_ms: int = 400) -> None:
        if (
            self._restoring_session
            or not self.autosave_enabled_var.get()
            or self.folder is None
        ):
            return
        if self._autosave_job is not None:
            self.root.after_cancel(self._autosave_job)
        self._autosave_job = self.root.after(
            delay_ms, self._save_autosave_now
        )
        self.autosave_status_var.set("自動保存: 変更あり")

    def _autosave_payload(self) -> dict:
        if self.folder is None:
            raise ValueError("画像フォルダが未設定です。")
        return {
            "schema_version": AUTOSAVE_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "image_folder": str(self.folder.resolve()),
            "image_count": len(self.images),
            "first_image": self.images[0].name if self.images else "",
            "last_image": self.images[-1].name if self.images else "",
            "current_before_file": (
                self.images[self.current_index].name
                if self.images and self.current_index < len(self.images)
                else ""
            ),
            "current_index": self.current_index,
            "mode": self.mode,
            "settings": {
                "fps": float(self.fps_var.get()),
                "unit": self.unit_var.get(),
                "real_length": float(self.real_length_var.get()),
                "frame_gap": self._frame_gap(),
                "brightness": float(self.brightness_var.get()),
                "contrast": float(self.contrast_var.get()),
                "show_before_position": bool(self.show_before_position_var.get()),
            },
            "calibration_points": [list(point) for point in self.calibration_points],
            "particle_clicks": {
                str(frame_index): list(point)
                for frame_index, point in self.particle_clicks.items()
            },
            "track_before": list(self.track_before) if self.track_before else None,
            "track_after": list(self.track_after) if self.track_after else None,
            "measurements": [asdict(measurement) for measurement in self.measurements],
        }

    def _save_autosave_now(self) -> bool:
        if self._autosave_job is not None:
            try:
                self.root.after_cancel(self._autosave_job)
            except tk.TclError:
                pass
        self._autosave_job = None
        if (
            not self.autosave_enabled_var.get()
            or self.folder is None
            or self._restoring_session
        ):
            return False
        try:
            target = self._autosave_path_for_folder(self.folder)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self._autosave_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(target)
        except (OSError, TypeError, ValueError, tk.TclError) as error:
            self.autosave_status_var.set("自動保存: 失敗")
            self.status_var.set(f"自動保存に失敗しました: {error}")
            return False
        saved_time = datetime.now().strftime("%H:%M:%S")
        self.autosave_status_var.set(f"自動保存: {saved_time}")
        return True

    def _load_autosave_data(self, folder: Path) -> dict | None:
        path = self._autosave_path_for_folder(folder)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if data.get("schema_version") != AUTOSAVE_SCHEMA_VERSION:
            return None
        return data

    def _restore_autosave_data(self, data: dict) -> bool:
        if not self.images:
            return False
        image_indices = {
            image.name.casefold(): index for index, image in enumerate(self.images)
        }
        settings = data.get("settings", {})
        try:
            self._restoring_session = True
            self.fps_var.set(float(settings.get("fps", self.fps_var.get())))
            unit = str(settings.get("unit", self.unit_var.get()))
            self.unit_var.set(unit if unit in {"mm", "cm", "m"} else "cm")
            self.real_length_var.set(
                float(settings.get("real_length", self.real_length_var.get()))
            )
            frame_gap = max(1, int(settings.get("frame_gap", 1)))
            self.frame_gap_var.set(frame_gap)
            self.brightness_var.set(
                min(max(float(settings.get("brightness", 1.0)), 0.2), 3.0)
            )
            self.contrast_var.set(
                min(max(float(settings.get("contrast", 1.0)), 0.2), 3.0)
            )
            self.show_before_position_var.set(
                bool(settings.get("show_before_position", True))
            )

            calibration = data.get("calibration_points", [])
            self.calibration_points = [
                (float(point[0]), float(point[1]))
                for point in calibration[:2]
                if isinstance(point, list) and len(point) == 2
            ]
            self.measurements = []
            for item in data.get("measurements", []):
                before_name = str(item["before_file"])
                after_name = str(item["after_file"])
                before_index = image_indices[before_name.casefold()]
                after_index = image_indices[after_name.casefold()]
                if after_index <= before_index:
                    continue
                measurement = Measurement(**item)
                measurement.before_frame = before_index + 1
                measurement.after_frame = after_index + 1
                measurement.before_file = self.images[before_index].name
                measurement.after_file = self.images[after_index].name
                self.measurements.append(measurement)

            self.particle_clicks = {}
            for frame_index, point in data.get("particle_clicks", {}).items():
                index = int(frame_index)
                if 0 <= index < len(self.images) and len(point) == 2:
                    self.particle_clicks[index] = (
                        float(point[0]), float(point[1])
                    )
            track_before = data.get("track_before")
            track_after = data.get("track_after")
            self.track_before = (
                (float(track_before[0]), float(track_before[1]))
                if isinstance(track_before, list) and len(track_before) == 2
                else None
            )
            self.track_after = (
                (float(track_after[0]), float(track_after[1]))
                if isinstance(track_after, list) and len(track_after) == 2
                else None
            )
            before_name = str(data.get("current_before_file", ""))
            current_index = image_indices.get(
                before_name.casefold(), int(data.get("current_index", 0))
            )
            self.current_index = min(
                max(0, current_index), max(0, len(self.images) - frame_gap - 1)
            )
            self.mode = (
                "measurement"
                if len(self.calibration_points) == 2
                else "calibration"
            )
        except (KeyError, TypeError, ValueError, tk.TclError):
            return False
        finally:
            self._restoring_session = False
        self._update_calibration_info()
        self._rebuild_table()
        self._apply_display_adjustments()
        return True

    def _build_ui(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="画像フォルダ", command=self.change_folder).pack(
            side="left"
        )
        ttk.Separator(toolbar, orient="vertical").pack(
            side="left", fill="y", padx=10
        )
        ttk.Label(toolbar, text="fps").pack(side="left")
        ttk.Spinbox(
            toolbar, textvariable=self.fps_var, from_=0.001, to=100000,
            increment=1, width=9
        ).pack(side="left", padx=(4, 10))
        ttk.Label(toolbar, text="コマ間隔").pack(side="left")
        gap = ttk.Spinbox(
            toolbar, textvariable=self.frame_gap_var, from_=1, to=1000,
            increment=1, width=6, command=self._gap_changed
        )
        gap.pack(side="left", padx=(4, 10))
        gap.bind("<FocusOut>", lambda _e: self._gap_changed())
        ttk.Label(toolbar, text="単位").pack(side="left")
        ttk.Combobox(
            toolbar, textvariable=self.unit_var,
            values=("mm", "cm", "m"), state="readonly", width=6
        ).pack(side="left", padx=(4, 10))
        ttk.Label(toolbar, text="校正長さ").pack(side="left")
        ttk.Entry(toolbar, textvariable=self.real_length_var, width=9).pack(
            side="left", padx=(4, 8)
        )
        ttk.Button(
            toolbar, text="表示を全体に合わせる", command=self.fit_images
        ).pack(side="right")
        ttk.Checkbutton(
            toolbar, text="自動保存",
            variable=self.autosave_enabled_var,
            command=self._autosave_toggled,
        ).pack(side="right", padx=(12, 6))
        ttk.Label(
            toolbar, textvariable=self.autosave_status_var,
            foreground="#475467"
        ).pack(side="right")

        instruction = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        instruction.pack(fill="x")
        self.calibration_button = ttk.Button(
            instruction, text="1  校正点を指定",
            command=self.start_calibration
        )
        self.calibration_button.pack(side="left")
        self.measure_button = ttk.Button(
            instruction, text="2  粒子位置を指定",
            command=self.start_measurement
        )
        self.measure_button.pack(side="left", padx=8)
        ttk.Button(
            instruction, text="現在の指定をクリア",
            command=self.clear_current_points
        ).pack(side="left")
        ttk.Label(
            instruction, textvariable=self.calibration_info_var,
            foreground="#175cd3"
        ).pack(side="left", padx=16)
        ttk.Checkbutton(
            instruction,
            text="after画像にbefore位置を表示",
            variable=self.show_before_position_var,
            command=self._show_before_position_toggled,
        ).pack(side="right", padx=(12, 0))
        ttk.Label(
            instruction,
            text="左クリック: 指定　ホイール: 拡大縮小　"
                 "右/中ドラッグ: 移動　←/→: 前/次",
            foreground="#667085"
        ).pack(side="right")

        display_controls = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        display_controls.pack(fill="x")
        ttk.Label(
            display_controls, text="画像表示", font=("", 10, "bold")
        ).pack(side="left")
        ttk.Label(display_controls, text="明るさ").pack(
            side="left", padx=(14, 4)
        )
        ttk.Scale(
            display_controls, from_=0.2, to=3.0,
            variable=self.brightness_var, length=190
        ).pack(side="left")
        ttk.Label(
            display_controls, textvariable=self.brightness_label_var, width=5
        ).pack(side="left", padx=(4, 12))
        ttk.Label(display_controls, text="コントラスト").pack(
            side="left", padx=(0, 4)
        )
        ttk.Scale(
            display_controls, from_=0.2, to=3.0,
            variable=self.contrast_var, length=190
        ).pack(side="left")
        ttk.Label(
            display_controls, textvariable=self.contrast_label_var, width=5
        ).pack(side="left", padx=(4, 8))
        ttk.Button(
            display_controls, text="表示調整をリセット",
            command=self.reset_display_adjustments
        ).pack(side="left")
        ttk.Label(
            display_controls,
            text="※表示のみ変更（元画像・計算値には影響しません）",
            foreground="#667085"
        ).pack(side="right")

        timeline = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        timeline.pack(fill="x")
        ttk.Label(
            timeline, text="フレーム移動", font=("", 10, "bold")
        ).pack(side="left")
        ttk.Button(
            timeline, text="先頭", command=self.go_to_first_pair, width=7
        ).pack(side="left", padx=(10, 6))
        self.seek_scale = ttk.Scale(
            timeline,
            from_=0,
            to=1,
            variable=self.seek_var,
            command=self._seekbar_changed,
            state="disabled",
        )
        self.seek_scale.pack(side="left", fill="x", expand=True)
        self.seek_scale.bind("<ButtonRelease-1>", self._seekbar_released)
        self.seek_scale.bind("<Left>", self._handle_left_arrow)
        self.seek_scale.bind("<Right>", self._handle_right_arrow)
        ttk.Button(
            timeline, text="末尾", command=self.go_to_last_pair, width=7
        ).pack(side="left", padx=(6, 12))
        ttk.Label(timeline, text="before番号").pack(side="left")
        self.frame_number_spinbox = ttk.Spinbox(
            timeline,
            textvariable=self.frame_number_var,
            from_=1,
            to=1,
            width=8,
            state="disabled",
        )
        self.frame_number_spinbox.pack(side="left", padx=(4, 4))
        self.frame_number_spinbox.bind(
            "<Return>", self._go_to_frame_from_event
        )
        ttk.Button(
            timeline, text="移動", command=self.go_to_frame_number, width=6
        ).pack(side="left", padx=(0, 10))
        ttk.Label(
            timeline, textvariable=self.seek_label_var, width=28,
            anchor="e", foreground="#475467"
        ).pack(side="right")

        paned = ttk.Panedwindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10)
        image_area = ttk.Frame(paned)
        result_area = ttk.Frame(paned)
        paned.add(image_area, weight=4)
        paned.add(result_area, weight=2)

        image_area.columnconfigure(0, weight=1)
        image_area.columnconfigure(1, weight=1)
        image_area.rowconfigure(0, weight=1)
        self.before_canvas = ImageCanvas(
            image_area, "before", self.handle_image_click, "before",
            self.get_overlays
        )
        self.before_canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.after_canvas = ImageCanvas(
            image_area, "after", self.handle_image_click, "after",
            self.get_overlays
        )
        self.after_canvas.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        action_row = ttk.Frame(result_area, padding=(0, 8))
        action_row.pack(fill="x")
        ttk.Label(
            action_row, textvariable=self.result_info_var,
            font=("", 11, "bold")
        ).pack(side="left")
        ttk.Button(
            action_row, text="記録して次へ  Enter",
            command=self.record_and_next
        ).pack(side="right")
        ttk.Button(
            action_row, text="同じペアに追加記録",
            command=self.record_same_pair
        ).pack(side="right", padx=8)
        ttk.Button(
            action_row, text="測定せず次へ（スキップ）",
            command=self.skip_to_next_pair
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            action_row, text="前の画像ペア", command=self.previous_pair
        ).pack(side="right")

        columns = (
            "number", "before", "after", "time", "displacement", "velocity"
        )
        self.table = ttk.Treeview(
            result_area, columns=columns, show="headings", height=7,
            selectmode="extended"
        )
        headings = {
            "number": "No.",
            "before": "before",
            "after": "after",
            "time": "時刻 (s)",
            "displacement": "1次元移動量",
            "velocity": "速度",
        }
        widths = {
            "number": 55, "before": 230, "after": 230,
            "time": 100, "displacement": 145, "velocity": 145
        }
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(
                column, width=widths[column],
                anchor="center" if column != "before" and column != "after" else "w"
            )
        scrollbar = ttk.Scrollbar(
            result_area, orient="vertical", command=self.table.yview
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<<TreeviewSelect>>", self._selection_changed)
        self.table.bind("<Double-1>", self._table_double_click)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")

        result_buttons = ttk.Frame(result_area, padding=(10, 0))
        result_buttons.pack(side="right", fill="y")
        ttk.Label(
            result_buttons, textvariable=self.summary_var,
            justify="left"
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            result_buttons, textvariable=self.selection_info_var,
            foreground="#475467"
        ).pack(anchor="w", pady=(0, 8))
        ttk.Button(
            result_buttons, text="グラフを表示", command=self.show_graph
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="CSV読込", command=self.import_csv
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="CSV出力", command=self.export_csv
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="グラフ画像出力", command=self.export_graph
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="自動保存先を開く",
            command=self.open_autosave_folder
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="選択行の画像へ移動",
            command=self.go_to_selected_result
        ).pack(fill="x", pady=(14, 2))
        ttk.Button(
            result_buttons, text="すべて選択  Ctrl+A",
            command=self.select_all_results
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="選択解除",
            command=self.clear_result_selection
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="選択行を削除", command=self.delete_selected
        ).pack(fill="x", pady=2)
        ttk.Button(
            result_buttons, text="測定結果をすべてクリア",
            command=self.clear_all_results
        ).pack(fill="x", pady=(14, 2))

        status = ttk.Label(
            self.root, textvariable=self.status_var,
            relief="sunken", anchor="w", padding=(8, 4)
        )
        status.pack(fill="x", side="bottom")

    def open_startup_dialog(self) -> bool:
        default_folder = Path(__file__).resolve().parent.parent / "フレーム"
        dialog = StartupDialog(self.root, default_folder)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return False
        self.fps_var.set(dialog.result["fps"])
        self.unit_var.set(dialog.result["unit"])
        return self.load_folder(dialog.result["folder"])

    def change_folder(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=str(self.folder) if self.folder else None,
            title="連続画像のフォルダを選択",
        )
        if selected:
            self.load_folder(Path(selected))

    def load_folder(self, folder: Path) -> bool:
        images = list_images(folder)
        if len(images) < 2:
            messagebox.showerror(
                APP_TITLE, "対応画像が2枚以上あるフォルダを選択してください。"
            )
            return False
        if self.measurements and not messagebox.askyesno(
            APP_TITLE,
            "画像フォルダを変更しますか？\n"
            "現在の測定結果は変更前に自動保存されます。"
        ):
            return False
        if self.folder is not None:
            self._save_autosave_now()
        autosave_data = self._load_autosave_data(folder)
        self.folder = folder
        self.images = images
        self.current_index = 0
        self.calibration_points.clear()
        self.track_before = None
        self.track_after = None
        self.particle_clicks.clear()
        self.measurements.clear()
        for item in self.table.get_children():
            self.table.delete(item)
        restored = False
        if autosave_data is not None:
            saved_at = autosave_data.get("saved_at", "日時不明")
            measurement_count = len(autosave_data.get("measurements", []))
            if messagebox.askyesno(
                APP_TITLE,
                "この画像フォルダの自動保存データが見つかりました。\n\n"
                f"保存日時: {saved_at}\n"
                f"測定数: {measurement_count}\n\n"
                "前回の作業を復元しますか？",
            ):
                restored = self._restore_autosave_data(autosave_data)
        if not restored:
            self.start_calibration()
        self._refresh_pair(fit=True)
        self._update_summary()
        if restored:
            self.status_var.set(
                f"自動保存から測定{len(self.measurements)}件と作業位置を復元しました。"
            )
            self.autosave_status_var.set("自動保存: 復元済")
        else:
            self.status_var.set(
                f"{len(images):,}枚を読み込みました。"
                "before画像で校正点を2点指定してください。"
            )
            self._schedule_autosave()
        return True

    def _frame_gap(self) -> int:
        try:
            return max(1, int(self.frame_gap_var.get()))
        except (ValueError, tk.TclError):
            return 1

    def _max_before_index(self) -> int:
        return max(0, len(self.images) - self._frame_gap() - 1)

    def _seek_label(self, before_index: int) -> str:
        if not self.images:
            return "画像未読込"
        gap = self._frame_gap()
        before_index = min(max(0, before_index), self._max_before_index())
        return (
            f"before {before_index + 1:,}/{len(self.images):,}　"
            f"after {before_index + gap + 1:,}/{len(self.images):,}"
        )

    def _sync_seekbar_from_index(self) -> None:
        self._updating_seekbar = True
        try:
            if not self.images:
                self.seek_scale.configure(from_=0, to=1, state="disabled")
                self.frame_number_spinbox.configure(
                    from_=1, to=1, state="disabled"
                )
                self.seek_var.set(0)
                self.frame_number_var.set(1)
                self.seek_label_var.set("画像未読込")
                return
            maximum = self._max_before_index()
            self.current_index = min(max(0, self.current_index), maximum)
            self.seek_scale.configure(
                from_=0,
                to=max(1, maximum),
                state="normal" if maximum > 0 else "disabled",
            )
            self.frame_number_spinbox.configure(
                from_=1, to=maximum + 1, state="normal"
            )
            self.seek_var.set(self.current_index)
            self.frame_number_var.set(self.current_index + 1)
            self.seek_label_var.set(self._seek_label(self.current_index))
        finally:
            self._updating_seekbar = False

    def _cancel_pending_seek(self) -> None:
        if self._seekbar_job is None:
            return
        try:
            self.root.after_cancel(self._seekbar_job)
        except tk.TclError:
            pass
        self._seekbar_job = None

    def _seekbar_changed(self, value: str) -> None:
        if self._updating_seekbar or not self.images:
            return
        try:
            target = int(round(float(value)))
        except (TypeError, ValueError):
            return
        target = min(max(0, target), self._max_before_index())
        self.seek_label_var.set(self._seek_label(target))

    def _seekbar_released(self, _event: tk.Event | None = None) -> str:
        self._cancel_pending_seek()
        self._apply_seekbar_position(int(round(self.seek_var.get())))
        return "break"

    def _apply_seekbar_position(self, index: int) -> None:
        self._seekbar_job = None
        if not self.images:
            return
        target = min(max(0, int(index)), self._max_before_index())
        if target == self.current_index:
            self._sync_seekbar_from_index()
            return
        self.current_index = target
        self.track_before = None
        self.track_after = None
        self._refresh_pair()
        self.status_var.set(
            f"シークバーで画像ペアへ移動しました（before {target + 1:,}枚目）。"
        )
        self._schedule_autosave()

    def go_to_first_pair(self) -> None:
        self._cancel_pending_seek()
        self._apply_seekbar_position(0)

    def go_to_last_pair(self) -> None:
        self._cancel_pending_seek()
        self._apply_seekbar_position(self._max_before_index())

    def _go_to_frame_from_event(self, _event: tk.Event) -> str:
        self.go_to_frame_number()
        return "break"

    def go_to_frame_number(self) -> None:
        if not self.images:
            return
        maximum = self._max_before_index() + 1
        try:
            frame_number = int(self.frame_number_var.get())
        except (ValueError, tk.TclError):
            messagebox.showinfo(
                APP_TITLE, f"before画像番号を1～{maximum:,}で入力してください。"
            )
            return
        if not 1 <= frame_number <= maximum:
            messagebox.showinfo(
                APP_TITLE, f"before画像番号を1～{maximum:,}で入力してください。"
            )
            self._sync_seekbar_from_index()
            return
        self._cancel_pending_seek()
        self._apply_seekbar_position(frame_number - 1)
        self.status_var.set(
            f"before画像{frame_number:,}枚目の画像ペアへ移動しました。"
        )

    def _gap_changed(self) -> None:
        if not self.images:
            return
        self._cancel_pending_seek()
        gap = self._frame_gap()
        self.frame_gap_var.set(gap)
        if self.current_index + gap >= len(self.images):
            self.current_index = max(0, len(self.images) - gap - 1)
        self.track_before = None
        self.track_after = None
        self._refresh_pair()
        self._schedule_autosave()

    def _refresh_pair(self, fit: bool = False) -> None:
        if not self.images:
            self._sync_seekbar_from_index()
            return
        gap = self._frame_gap()
        after_index = self.current_index + gap
        if after_index >= len(self.images):
            return
        before_path = self.images[self.current_index]
        after_path = self.images[after_index]
        self.before_canvas.set_image(
            before_path,
            f"before  [{self.current_index + 1}/{len(self.images)}]  {before_path.name}",
            preserve_view=not fit,
        )
        self.after_canvas.set_image(
            after_path,
            f"after  [{after_index + 1}/{len(self.images)}]  {after_path.name}",
            preserve_view=not fit,
        )
        self._sync_seekbar_from_index()
        if fit:
            self.fit_images()
        self._update_result_preview()

    def fit_images(self) -> None:
        self.before_canvas.fit()
        self.after_canvas.fit()

    def start_calibration(self) -> None:
        self.mode = "calibration"
        self.calibration_points.clear()
        self.track_before = None
        self.track_after = None
        self.calibration_info_var.set(
            "再校正中（2点の確定後に結果を更新）"
            if self.measurements else "未校正"
        )
        self.status_var.set(
            "校正モード: before画像上で、実際の長さが分かる2点を順にクリックしてください。"
        )
        self._redraw_both()
        self._schedule_autosave()

    def start_measurement(self) -> None:
        if len(self.calibration_points) != 2:
            messagebox.showinfo(
                APP_TITLE, "先にbefore画像上で校正点を2点指定してください。"
            )
            return
        try:
            if float(self.real_length_var.get()) <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showerror(APP_TITLE, "校正長さは0より大きくしてください。")
            return
        self.mode = "measurement"
        self.track_before = None
        self.track_after = None
        self._clear_particle_clicks_for_current_pair()
        self.status_var.set(
            "粒子指定モード: beforeとafterで同じ粒子を1回ずつクリックしてください。"
        )
        self._redraw_both()
        self._schedule_autosave()

    def handle_image_click(
        self, side: str, point: tuple[float, float]
    ) -> None:
        if self.mode == "calibration":
            if side != "before":
                self.status_var.set("校正点はbefore画像上で指定してください。")
                return
            if len(self.calibration_points) >= 2:
                self.calibration_points.clear()
            self.calibration_points.append(point)
            if len(self.calibration_points) == 2:
                try:
                    real_length = float(self.real_length_var.get())
                    if real_length <= 0:
                        raise ValueError
                    projected_displacement(
                        self.calibration_points[0],
                        self.calibration_points[1],
                        self.calibration_points[0],
                        self.calibration_points[1],
                        real_length,
                    )
                except (ValueError, tk.TclError):
                    self.calibration_points.pop()
                    messagebox.showerror(
                        APP_TITLE, "校正長さは0より大きくしてください。"
                    )
                    return
                self._update_calibration_info()
                self.mode = "measurement"
                self._recalculate_measurements()
                self.status_var.set(
                    "校正完了。保存済みの結果も新しい校正値で更新しました。"
                    if self.measurements else
                    "校正完了。beforeとafterで同じ粒子をクリックしてください。"
                )
            else:
                self.status_var.set("校正点1を指定しました。校正点2をクリックしてください。")
        else:
            if side == "before":
                self.track_before = point
                self.particle_clicks[self.current_index] = point
            else:
                self.track_after = point
                self.particle_clicks[
                    self.current_index + self._frame_gap()
                ] = point
            if self.track_before is None:
                self.status_var.set("次にbefore画像で同じ粒子を指定してください。")
            elif self.track_after is None:
                self.status_var.set("次にafter画像で同じ粒子を指定してください。")
            else:
                self.status_var.set(
                    "粒子位置を指定しました。値を確認して「記録して次へ」を押してください。"
                )
        self._redraw_both()
        self._update_result_preview()
        self._schedule_autosave()

    def _measurements_for_current_pair(self) -> list[Measurement]:
        if not self.images:
            return []
        before_frame = self.current_index + 1
        after_frame = before_frame + self._frame_gap()
        return [
            measurement for measurement in self.measurements
            if measurement.before_frame == before_frame
            and measurement.after_frame == after_frame
        ]

    def get_overlays(self, side: str) -> list[dict]:
        overlays: list[dict] = []
        if len(self.calibration_points) == 2:
            overlays.append({
                "type": "line", "p1": self.calibration_points[0],
                "p2": self.calibration_points[1], "color": "#29b6f6",
                "dash": (8, 5),
            })
        for index, point in enumerate(self.calibration_points):
            overlays.append({
                "type": "point", "p": point, "color": "#29b6f6",
                "label": f"校正{index + 1}",
            })
        for measurement in self._measurements_for_current_pair():
            point = (
                (measurement.before_x_px, measurement.before_y_px)
                if side == "before"
                else (measurement.after_x_px, measurement.after_y_px)
            )
            overlays.append({
                "type": "point", "p": point,
                "color": "#ffd54f" if side == "before" else "#ffb74d",
                "label": f"記録No.{measurement.number}",
            })
        if (
            side == "after"
            and self.mode == "measurement"
            and self.show_before_position_var.get()
        ):
            before_point, _ = self._current_particle_points()
            if before_point is not None:
                overlays.append({
                    "type": "cross",
                    "p": before_point,
                    "color": "#e040fb",
                    "label": "before位置（参考）",
                })
        track = self.track_before if side == "before" else self.track_after
        if track is not None:
            overlays.append({
                "type": "point", "p": track,
                "color": "#00e676" if side == "before" else "#ff5252",
                "label": "粒子",
            })
        return overlays

    def clear_current_points(self) -> None:
        if self.mode == "calibration":
            self.calibration_points.clear()
            self.calibration_info_var.set("未校正")
        else:
            self.track_before = None
            self.track_after = None
            self._clear_particle_clicks_for_current_pair()
        self._update_result_preview()
        self._redraw_both()
        self._schedule_autosave()

    def _clear_particle_clicks_for_current_pair(self) -> None:
        self.particle_clicks.pop(self.current_index, None)
        self.particle_clicks.pop(
            self.current_index + self._frame_gap(), None
        )

    def _current_particle_points(
        self,
    ) -> tuple[
        tuple[float, float] | None,
        tuple[float, float] | None,
    ]:
        before = self.track_before
        after = self.track_after
        if before is None:
            before = self.particle_clicks.get(self.current_index)
        if after is None:
            after = self.particle_clicks.get(
                self.current_index + self._frame_gap()
            )
        return before, after

    def _current_calculation(self) -> tuple[float, float, float] | None:
        before, after = self._current_particle_points()
        if (
            len(self.calibration_points) != 2
            or before is None
            or after is None
        ):
            return None
        try:
            return calculate_velocity(
                before,
                after,
                self.calibration_points[0],
                self.calibration_points[1],
                float(self.real_length_var.get()),
                float(self.fps_var.get()),
                self._frame_gap(),
            )
        except (ValueError, tk.TclError):
            return None

    def _update_result_preview(self) -> None:
        calculation = self._current_calculation()
        if calculation is None:
            before, after = self._current_particle_points()
            calibration_text = (
                "校正済" if len(self.calibration_points) == 2 else "校正なし"
            )
            before_text = "before指定済" if before is not None else "before未指定"
            after_text = "after指定済" if after is not None else "after未指定"
            self.result_info_var.set(
                f"{calibration_text} ｜ {before_text} ｜ {after_text}"
            )
            return
        displacement, velocity, _ = calculation
        unit = self.unit_var.get()
        self.result_info_var.set(
            f"移動量 {displacement:+.5g} {unit}　"
            f"速度 {velocity:+.5g} {unit}/s"
        )

    def _calculation_setting_changed(self, *_args) -> None:
        if self._restoring_session:
            return
        if self._recalculation_job is not None:
            self.root.after_cancel(self._recalculation_job)
        self._recalculation_job = self.root.after_idle(
            self._apply_calculation_setting_change
        )

    def _apply_calculation_setting_change(self) -> None:
        if self._recalculation_job is not None:
            try:
                self.root.after_cancel(self._recalculation_job)
            except tk.TclError:
                pass
        self._recalculation_job = None
        self._update_calibration_info()
        self._update_result_preview()
        self._recalculate_measurements()
        self._schedule_autosave()

    def _update_calibration_info(self) -> None:
        if len(self.calibration_points) != 2:
            return
        try:
            real_length = float(self.real_length_var.get())
            if real_length <= 0:
                return
        except (ValueError, tk.TclError):
            return
        pixel_length = math.hypot(
            self.calibration_points[1][0] - self.calibration_points[0][0],
            self.calibration_points[1][1] - self.calibration_points[0][1],
        )
        self.calibration_info_var.set(
            f"校正: {pixel_length:.2f} px = "
            f"{real_length:g} {self.unit_var.get()}　点1→点2が正方向"
        )

    def _recalculate_measurements(self) -> None:
        if len(self.calibration_points) != 2 or not self.measurements:
            return
        try:
            real_length = float(self.real_length_var.get())
            fps = float(self.fps_var.get())
            if real_length <= 0 or fps <= 0:
                return
        except (ValueError, tk.TclError):
            return
        unit = self.unit_var.get()
        for measurement in self.measurements:
            frame_gap = measurement.after_frame - measurement.before_frame
            displacement, velocity, _ = calculate_velocity(
                (measurement.before_x_px, measurement.before_y_px),
                (measurement.after_x_px, measurement.after_y_px),
                self.calibration_points[0],
                self.calibration_points[1],
                real_length,
                fps,
                frame_gap,
            )
            measurement.time_s = (measurement.after_frame - 1) / fps
            measurement.delta_time_s = frame_gap / fps
            measurement.displacement = displacement
            measurement.velocity = velocity
            measurement.unit = unit
        self._rebuild_table()
        self.status_var.set(
            f"校正・時間設定の変更を測定結果{len(self.measurements)}件へ反映しました。"
        )

    def record_measurement(self, quiet: bool = False) -> bool:
        before_point, after_point = self._current_particle_points()
        calculation = self._current_calculation()
        if calculation is None:
            if not quiet:
                missing: list[str] = []
                if len(self.calibration_points) != 2:
                    missing.append("・校正点2点")
                if before_point is None:
                    missing.append("・before画像の粒子位置")
                if after_point is None:
                    missing.append("・after画像の粒子位置")
                if not missing:
                    missing.append("・有効な校正長さ、fps、コマ間隔")
                messagebox.showinfo(
                    APP_TITLE,
                    "記録に必要な指定が不足しています。\n\n"
                    + "\n".join(missing)
                )
            return False
        try:
            fps = float(self.fps_var.get())
            if fps <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showerror(
                APP_TITLE, "フレームレートは0より大きくしてください。"
            )
            return False
        gap = self._frame_gap()
        after_index = self.current_index + gap
        displacement, velocity, _ = calculation
        measurement = Measurement(
            number=len(self.measurements) + 1,
            before_frame=self.current_index + 1,
            after_frame=after_index + 1,
            before_file=self.images[self.current_index].name,
            after_file=self.images[after_index].name,
            time_s=after_index / fps,
            delta_time_s=gap / fps,
            displacement=displacement,
            velocity=velocity,
            unit=self.unit_var.get(),
            before_x_px=before_point[0],
            before_y_px=before_point[1],
            after_x_px=after_point[0],
            after_y_px=after_point[1],
        )
        self.measurements.append(measurement)
        item = self.table.insert(
            "", "end",
            values=(
                measurement.number,
                measurement.before_file,
                measurement.after_file,
                f"{measurement.time_s:.6f}",
                f"{measurement.displacement:+.6g} {measurement.unit}",
                f"{measurement.velocity:+.6g} {measurement.unit}/s",
            ),
        )
        self.table.see(item)
        self._update_summary()
        self.status_var.set(f"測定No.{measurement.number}を記録しました。")
        self._schedule_autosave()
        return True

    def record_same_pair(self) -> None:
        if not self.record_measurement():
            return
        self.track_before = None
        self.track_after = None
        self._clear_particle_clicks_for_current_pair()
        self._update_result_preview()
        self._redraw_both()
        self.status_var.set(
            "この画像ペアへ測定を追加しました。次の粒子をbeforeとafterで指定できます。"
        )

    def record_and_next(self) -> None:
        self._cancel_pending_seek()
        _, previous_after = self._current_particle_points()
        if not self.record_measurement():
            return
        gap = self._frame_gap()
        next_index = self.current_index + gap
        if next_index + gap >= len(self.images):
            self.status_var.set("測定を記録しました。最後の画像ペアです。")
            self.track_before = None
            self.track_after = None
            self._redraw_both()
            return
        self.current_index = next_index
        # The previous after image is the new before image, so carry its point forward.
        self.track_before = previous_after
        self.track_after = None
        if previous_after is not None:
            self.particle_clicks[self.current_index] = previous_after
        self._refresh_pair()
        self.status_var.set(
            "次の画像ペアへ進みました。前回のafter粒子位置をbeforeへ引き継ぎました。"
        )
        self._schedule_autosave()

    def skip_to_next_pair(self) -> None:
        self._cancel_pending_seek()
        if not self.images:
            return
        gap = self._frame_gap()
        next_index = self.current_index + gap
        if next_index + gap >= len(self.images):
            self.status_var.set("最後の画像ペアのため、これ以上先へ進めません。")
            return
        self.current_index = next_index
        self.track_before = None
        self.track_after = None
        self._refresh_pair()
        existing_count = len(self._measurements_for_current_pair())
        if existing_count:
            self.status_var.set(
                "次の画像ペアへ移動しました。"
                f"保存済みの粒子位置{existing_count}件を黄色で表示しています。"
            )
        else:
            self.status_var.set(
                "測定結果を記録せず、次の画像ペアへスキップしました。"
            )
        self._schedule_autosave()

    def previous_pair(self) -> None:
        self._cancel_pending_seek()
        gap = self._frame_gap()
        if self.current_index <= 0:
            self.status_var.set("先頭の画像ペアです。")
            return
        self.current_index = max(0, self.current_index - gap)
        self.track_before = None
        self.track_after = None
        self._refresh_pair()
        existing_count = len(self._measurements_for_current_pair())
        if existing_count:
            self.status_var.set(
                "1組前の画像ペアへ戻りました。"
                f"保存済みの粒子位置{existing_count}件を黄色で表示しています。"
            )
        else:
            self.status_var.set("1組前の画像ペアへ戻りました。")
        self._schedule_autosave()

    def _update_summary(self) -> None:
        if not self.measurements:
            self.summary_var.set("測定数 0")
            return
        velocities = [m.velocity for m in self.measurements]
        unit = self.measurements[-1].unit
        self.summary_var.set(
            f"測定数 {len(velocities)}\n"
            f"平均 {sum(velocities) / len(velocities):+.5g} {unit}/s\n"
            f"最小 {min(velocities):+.5g} {unit}/s\n"
            f"最大 {max(velocities):+.5g} {unit}/s"
        )

    def _selection_changed(self, _event: tk.Event | None = None) -> None:
        self.selection_info_var.set(f"選択 {len(self.table.selection())}件")

    def _table_double_click(self, event: tk.Event) -> str | None:
        item = self.table.identify_row(event.y)
        if not item:
            return None
        self.table.selection_set(item)
        self.table.focus(item)
        self.go_to_selected_result()
        return "break"

    def go_to_selected_result(self) -> None:
        self._cancel_pending_seek()
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "移動する測定結果を1行選択してください。")
            return
        focused = self.table.focus()
        item = focused if focused in selected else selected[0]
        result_index = self.table.index(item)
        if result_index >= len(self.measurements):
            return
        measurement = self.measurements[result_index]
        frame_gap = measurement.after_frame - measurement.before_frame
        self.frame_gap_var.set(frame_gap)
        self.current_index = measurement.before_frame - 1
        self.track_before = (
            measurement.before_x_px, measurement.before_y_px
        )
        self.track_after = (
            measurement.after_x_px, measurement.after_y_px
        )
        self.mode = "measurement"
        self._refresh_pair()
        self.status_var.set(
            f"測定No.{measurement.number}の画像ペアへ移動しました。"
            "記録時の粒子位置も表示しています。"
        )
        self._schedule_autosave()

    def select_all_results(self) -> str:
        items = self.table.get_children()
        if items:
            self.table.selection_set(items)
            self.table.focus(items[0])
        self._selection_changed()
        return "break"

    def clear_result_selection(self) -> None:
        self.table.selection_remove(self.table.selection())
        self._selection_changed()

    def delete_selected(self) -> None:
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "削除する測定結果を選択してください。")
            return
        indices = sorted(
            (self.table.index(item) for item in selected), reverse=True
        )
        for index in indices:
            del self.measurements[index]
        for item in selected:
            self.table.delete(item)
        self._rebuild_table()
        self._selection_changed()
        self._schedule_autosave()

    def clear_all_results(self) -> None:
        if not self.measurements:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"測定結果{len(self.measurements)}件をすべてクリアしますか？",
        ):
            return
        self.measurements.clear()
        for item in self.table.get_children():
            self.table.delete(item)
        self._update_summary()
        self._selection_changed()
        self.status_var.set("測定結果をすべてクリアしました。")
        self._schedule_autosave()

    def _rebuild_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for number, measurement in enumerate(self.measurements, start=1):
            measurement.number = number
            self.table.insert(
                "", "end",
                values=(
                    measurement.number, measurement.before_file,
                    measurement.after_file, f"{measurement.time_s:.6f}",
                    f"{measurement.displacement:+.6g} {measurement.unit}",
                    f"{measurement.velocity:+.6g} {measurement.unit}/s",
                ),
            )
        self._update_summary()

    def import_csv(self) -> None:
        if not self.images:
            messagebox.showinfo(APP_TITLE, "先に画像フォルダを読み込んでください。")
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="測定結果CSVを読み込む",
            filetypes=[("CSV", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        replace_existing = True
        if self.measurements:
            choice = messagebox.askyesnocancel(
                APP_TITLE,
                "現在の測定結果があります。\n\n"
                "「はい」: 現在の結果を置き換える\n"
                "「いいえ」: 現在の結果へ追加する\n"
                "「キャンセル」: 読み込みを中止する",
            )
            if choice is None:
                return
            replace_existing = choice
        try:
            imported, skipped = self._read_measurements_csv(Path(path))
        except (OSError, UnicodeError, csv.Error, ValueError) as error:
            messagebox.showerror(
                APP_TITLE, f"CSVを読み込めませんでした。\n\n{error}"
            )
            return
        if not imported:
            messagebox.showerror(
                APP_TITLE,
                "読み込める測定結果がありませんでした。\n"
                "CSV内の画像名が現在の画像フォルダと一致するか確認してください。",
            )
            return
        if replace_existing:
            self.measurements = imported
        else:
            self.measurements.extend(imported)
        self._rebuild_table()
        self.clear_result_selection()
        self._redraw_both()
        message = f"CSVから測定結果{len(imported)}件を読み込みました。"
        if skipped:
            message += f" 画像名不一致または不正な行{skipped}件は除外しました。"
        self.status_var.set(message)
        self._schedule_autosave()
        messagebox.showinfo(APP_TITLE, message)

    def _read_measurements_csv(
        self, path: Path
    ) -> tuple[list[Measurement], int]:
        required_fields = {
            "before_file", "after_file", "displacement", "velocity", "unit",
            "before_x_px", "before_y_px", "after_x_px", "after_y_px",
        }
        image_indices = {
            image.name.casefold(): index for index, image in enumerate(self.images)
        }
        imported: list[Measurement] = []
        skipped = 0
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None:
                raise ValueError("CSVに見出し行がありません。")
            missing = required_fields.difference(reader.fieldnames)
            if missing:
                raise ValueError(
                    "必要な列がありません: " + ", ".join(sorted(missing))
                )
            for row in reader:
                try:
                    before_name = row["before_file"].strip()
                    after_name = row["after_file"].strip()
                    before_index = image_indices[before_name.casefold()]
                    after_index = image_indices[after_name.casefold()]
                    before_file = self.images[before_index].name
                    after_file = self.images[after_index].name
                    frame_gap = after_index - before_index
                    if frame_gap <= 0:
                        raise ValueError
                    fps = float(self.fps_var.get())
                    time_s = (
                        float(row["time_s"])
                        if row.get("time_s") not in (None, "")
                        else after_index / fps
                    )
                    delta_time_s = (
                        float(row["delta_time_s"])
                        if row.get("delta_time_s") not in (None, "")
                        else frame_gap / fps
                    )
                    measurement = Measurement(
                        number=len(imported) + 1,
                        before_frame=before_index + 1,
                        after_frame=after_index + 1,
                        before_file=before_file,
                        after_file=after_file,
                        time_s=time_s,
                        delta_time_s=delta_time_s,
                        displacement=float(row["displacement"]),
                        velocity=float(row["velocity"]),
                        unit=row["unit"].strip() or self.unit_var.get(),
                        before_x_px=float(row["before_x_px"]),
                        before_y_px=float(row["before_y_px"]),
                        after_x_px=float(row["after_x_px"]),
                        after_y_px=float(row["after_y_px"]),
                    )
                except (KeyError, TypeError, ValueError, tk.TclError):
                    skipped += 1
                    continue
                imported.append(measurement)
        return imported, skipped

    def export_csv(self) -> None:
        if not self.measurements:
            messagebox.showinfo(APP_TITLE, "出力する測定結果がありません。")
            return
        initial = "flow_velocity_results.csv"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="測定結果をCSV出力",
            defaultextension=".csv", initialfile=initial,
            filetypes=[("CSV", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        fieldnames = list(asdict(self.measurements[0]).keys())
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for measurement in self.measurements:
                writer.writerow(asdict(measurement))
        self.status_var.set(f"CSVを出力しました: {path}")

    def _create_graph_image(self, width: int = 1200, height: int = 700) -> Image.Image:
        if not self.measurements:
            raise ValueError("測定結果がありません。")
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        margin_left, margin_right = 105, 45
        margin_top, margin_bottom = 55, 90
        plot_left, plot_top = margin_left, margin_top
        plot_right, plot_bottom = width - margin_right, height - margin_bottom

        xs = [m.time_s for m in self.measurements]
        ys = [m.velocity for m in self.measurements]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        if x_min == x_max:
            x_min -= 0.5
            x_max += 0.5
        if y_min == y_max:
            pad = max(abs(y_min) * 0.1, 1.0)
            y_min -= pad
            y_max += pad
        else:
            pad = (y_max - y_min) * 0.1
            y_min -= pad
            y_max += pad

        def tx(value: float) -> float:
            return plot_left + (value - x_min) / (x_max - x_min) * (
                plot_right - plot_left
            )

        def ty(value: float) -> float:
            return plot_bottom - (value - y_min) / (y_max - y_min) * (
                plot_bottom - plot_top
            )

        for i in range(6):
            x = plot_left + (plot_right - plot_left) * i / 5
            value = x_min + (x_max - x_min) * i / 5
            draw.line((x, plot_top, x, plot_bottom), fill="#e4e7ec", width=1)
            label = f"{value:.4g}"
            draw.text((x, plot_bottom + 12), label, fill="#344054",
                      font=font, anchor="ma")
        for i in range(6):
            y = plot_bottom - (plot_bottom - plot_top) * i / 5
            value = y_min + (y_max - y_min) * i / 5
            draw.line((plot_left, y, plot_right, y), fill="#e4e7ec", width=1)
            draw.text((plot_left - 12, y), f"{value:.4g}", fill="#344054",
                      font=font, anchor="rm")
        if y_min <= 0 <= y_max:
            draw.line(
                (plot_left, ty(0), plot_right, ty(0)),
                fill="#98a2b3", width=2
            )
        points = [(tx(x), ty(y)) for x, y in zip(xs, ys)]
        if len(points) > 1:
            draw.line(points, fill="#175cd3", width=3)
        for x, y in points:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5),
                         fill="#175cd3", outline="white", width=2)
        draw.rectangle(
            (plot_left, plot_top, plot_right, plot_bottom),
            outline="#475467", width=2
        )
        unit = self.measurements[-1].unit
        draw.text(
            (width / 2, 22), "Manual flow velocity measurement",
            fill="#101828", font=font, anchor="ma"
        )
        draw.text(
            (width / 2, height - 35), "Time from first loaded frame (s)",
            fill="#101828", font=font, anchor="ma"
        )
        draw.text(
            (18, height / 2), f"Velocity ({unit}/s)",
            fill="#101828", font=font, anchor="lm"
        )
        return image

    def show_graph(self) -> None:
        if not self.measurements:
            messagebox.showinfo(APP_TITLE, "表示する測定結果がありません。")
            return
        graph = self._create_graph_image(1000, 580)
        window = tk.Toplevel(self.root)
        window.title("速度グラフ")
        photo = ImageTk.PhotoImage(graph)
        label = ttk.Label(window, image=photo)
        label.image = photo
        label.pack(padx=10, pady=10)

    def export_graph(self) -> None:
        if not self.measurements:
            messagebox.showinfo(APP_TITLE, "出力する測定結果がありません。")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="速度グラフを画像出力",
            defaultextension=".png", initialfile="flow_velocity_graph.png",
            filetypes=[("PNG画像", "*.png")],
        )
        if not path:
            return
        self._create_graph_image().save(path)
        self.status_var.set(f"グラフ画像を出力しました: {path}")

    def _redraw_both(self) -> None:
        self.before_canvas.redraw()
        self.after_canvas.redraw()

    def open_autosave_folder(self) -> None:
        try:
            self.autosave_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(self.autosave_dir)
        except OSError as error:
            messagebox.showerror(
                APP_TITLE, f"自動保存先を開けませんでした。\n\n{error}"
            )

    def _cancel_pending_jobs(self) -> None:
        for attribute in (
            "_recalculation_job",
            "_display_adjustment_job",
            "_autosave_job",
            "_seekbar_job",
        ):
            job = getattr(self, attribute)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)

    def close(self) -> None:
        self._save_autosave_now()
        if self.measurements:
            message = (
                "終了しますか？\n\n測定結果と作業状態は自動保存されています。\n"
                "共有・集計用のCSVは「CSV出力」から別途保存してください。"
                if self.autosave_enabled_var.get()
                else "終了しますか？ 未出力の測定結果は保存されません。"
            )
            if not messagebox.askyesno(APP_TITLE, message):
                return
        self._cancel_pending_jobs()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    root.report_callback_exception = report_gui_exception
    app = FlowVelocityApp(root)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    if not app.open_startup_dialog():
        root.destroy()
        return 0
    root.mainloop()
    return 0


def run_self_test(image_path: str | None = None) -> int:
    displacement, velocity, _ = calculate_velocity(
        before=(0, 0),
        after=(10, 50),
        calibration_start=(0, 0),
        calibration_end=(100, 0),
        real_length=10,
        fps=240,
        frame_gap=1,
    )
    if not math.isclose(displacement, 1.0) or not math.isclose(velocity, 240.0):
        return 2
    if image_path:
        with Image.open(image_path) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                return 3
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    root.destroy()
    return 0


def report_gui_exception(
    exception_type: type[BaseException],
    exception: BaseException,
    exception_traceback,
) -> None:
    details = "".join(
        traceback.format_exception(exception_type, exception, exception_traceback)
    )
    log_path = error_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(details, encoding="utf-8")
    except OSError:
        pass
    messagebox.showerror(
        APP_TITLE,
        f"アプリでエラーが発生しました。\n\n{exception}\n\n"
        f"詳細: {log_path}",
    )


if __name__ == "__main__":
    try:
        if "--self-test" in sys.argv:
            image_argument = None
            argument_index = sys.argv.index("--self-test")
            if argument_index + 1 < len(sys.argv):
                image_argument = sys.argv[argument_index + 1]
            sys.exit(run_self_test(image_argument))
        sys.exit(main())
    except Exception as error:
        details = traceback.format_exc()
        log_path = error_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(details, encoding="utf-8")
        except OSError:
            pass
        if sys.stderr is not None:
            print(details, file=sys.stderr)
        try:
            error_root = tk.Tk()
            error_root.withdraw()
            messagebox.showerror(
                APP_TITLE,
                f"起動できませんでした。\n\n{error}\n\n詳細: {log_path}",
                parent=error_root,
            )
            error_root.destroy()
        finally:
            sys.exit(1)
