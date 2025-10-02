#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""リアルタイム温度・電力モニター GUI"""

import sys
import os
import time
import math
import datetime as dt
import threading
import queue
import tkinter as tk
import tkinter.font as tkfont
from typing import Dict, List, Optional

import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from PIL import Image, ImageTk

import japanize_matplotlib  # noqa: F401

from compowayf_driver import CompoWayFDriver


# ==== シリアル・計測設定 ====
PORT = "/dev/ttyUSB0"
E5CD_NODE = "01"
CURRENT_NODE = "02"
SID = "0"
POLL_MS = 0
VOLTAGE_V = 200.0  # 指定通り電圧は固定


# ---- デザイン設定 ----
BG_COLOR = "#060b16"
PANEL_COLOR = "#0e1626"
ACCENT_COLOR = "#ef4444"
TEXT_PRIMARY = "#f8fafc"
TEXT_SECONDARY = "#94a3b8"
GRID_COLOR = "#1f2a44"
TEMP_COLOR = "#ef4444"
POWER_COLOR = "#2563eb"


# ---- 画像パス ----
script_dir = os.path.dirname(os.path.abspath(__file__))
DEVICE1_IMAGE_PATH = os.path.join(script_dir, "product_1.png")
DEVICE2_IMAGE_PATH = os.path.join(script_dir, "product_2.png")
LOGO_IMAGE_PATH = os.path.join(script_dir, "Leister_Logo_hq.png")


class App(tk.Tk):
    """温度・電力量を表示する Tkinter アプリ"""

    def __init__(self) -> None:
        super().__init__()
        self.title("HeatCycle Monitor")
        self.configure(bg=BG_COLOR)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        matplotlib.rcParams.update(
            {
                "axes.titlesize": 26,
                "axes.labelsize": 20,
                "xtick.labelsize": 16,
                "ytick.labelsize": 16,
            }
        )

        try:
            default_font = tkfont.nametofont("TkDefaultFont")
            default_font.configure(family="Yu Gothic UI", size=16)
        except tk.TclError:
            pass

        self.t0 = dt.datetime.now()
        self.temp_times: List[dt.datetime] = []
        self.temp_values: List[float] = []
        self.current_times: List[dt.datetime] = []
        self.currents: List[float] = []
        self.power_times: List[dt.datetime] = []
        self.power_values: List[float] = []

        # センサー未接続でも GUI は起動させる
        try:
            self.cwf = CompoWayFDriver(port=PORT)
        except Exception:
            self.cwf = None

        self.io_lock = threading.Lock()

        # レイアウト：左(80%) / 右(20%)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)
        self.rowconfigure(0, weight=1)

        self._right_ratio = 0.20
        self.bind("<Configure>", self._on_root_resize)

        # 右ペインの縦配分（上から: 設定温度 / デバイス1 / デバイス2 / 余白(0)）
        self._section_ratios = (0.17, 0.38, 0.35, 0.1)

        # 画像ハンドリング
        self._img_sources: Dict[int, Optional["Image.Image"]] = {}
        self._img_labels: Dict[int, tk.Label] = {}
        self._caption_labels: Dict[int, Optional[tk.Label]] = {}
        self._img_tk_cache: Dict[int, object] = {}
        self._pil_available = Image is not None and ImageTk is not None

        # 左（グラフ）
        left_frame = tk.Frame(self, bg=BG_COLOR)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(16, 6), pady=16)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)

        # 右（設定温度 + デバイス画像2つ）※ロゴはここに置かない
        right_frame = tk.Frame(self, bg=BG_COLOR)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 16), pady=16)
        right_frame.columnconfigure(0, weight=1)
        for row in range(4):
            right_frame.rowconfigure(row, weight=1)
        right_frame.grid_propagate(False)
        right_frame.bind("<Configure>", self._on_right_frame_resize)
        self.right_frame = right_frame

        self._build_graph_area(left_frame)
        self._build_setpoint_panel(right_frame)
        self._build_showcase(right_frame)  # デバイス1/2のみ

        # === 企業ロゴはウィンドウ直下に直接配置 ===
        self.logo_image_pil: Optional["Image.Image"] = self._load_image_pil(LOGO_IMAGE_PATH)
        self.logo_label = tk.Label(self, bg=BG_COLOR, bd=0, highlightthickness=0)
        self._logo_tk_ref: Optional[object] = None  # GC防止用参照

        # ポーリングスレッド（無ければ起動しない）
        self.result_q = queue.Queue()
        self.stop_evt = threading.Event()
        if self.cwf is not None:
            self.worker = threading.Thread(target=self.poll_worker, daemon=True)
            self.worker.start()
        else:
            self.worker = None

        self.after(100, self.drain_results)

        # フルスクリーン起動 + 解除/トグル
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.bind("<F11>", self.toggle_fullscreen)

        # 初期ロゴ配置
        self.after(0, self._update_logo_position)

    # ------------------------------------------------------------------
    def toggle_fullscreen(self, event=None):
        """F11キーでフルスクリーン ⇄ 通常表示を切り替え"""
        is_full = self.attributes("-fullscreen")
        self.attributes("-fullscreen", not is_full)

    # ------------------------------------------------------------------
    def _build_graph_area(self, parent: tk.Frame) -> None:
        fig = Figure(figsize=(12.4, 8.2), dpi=100)
        fig.patch.set_facecolor(BG_COLOR)
        gs = fig.add_gridspec(2, 1, hspace=0.32)
        self.ax_temp = fig.add_subplot(gs[0])
        self.ax_power = fig.add_subplot(gs[1], sharex=self.ax_temp)
        fig.subplots_adjust(left=0.1, right=0.95, top=0.92, bottom=0.08)

        for ax in (self.ax_temp, self.ax_power):
            ax.set_facecolor(PANEL_COLOR)
            ax.tick_params(axis="x", colors=TEXT_PRIMARY, labelsize=16, width=1.8, length=8, pad=10)
            ax.tick_params(axis="y", colors=TEXT_PRIMARY, labelsize=16, width=1.8, length=8, pad=10)
            for spine in ax.spines.values():
                spine.set_color("#1e293b")
            ax.grid(True, color=GRID_COLOR, alpha=0.55, linewidth=1.2)

        self.ax_temp.set_ylabel("温度 [℃]", color=TEXT_PRIMARY, labelpad=18)
        elapsed_formatter = FuncFormatter(self._format_elapsed_time)
        self.ax_temp.xaxis.set_major_formatter(elapsed_formatter)
        self.ax_temp.tick_params(axis="x", which="both", labelbottom=False)

        self.ax_power.set_ylabel("平均消費電力 [W]", color=TEXT_PRIMARY, labelpad=20)
        self.ax_power.xaxis.set_major_formatter(elapsed_formatter)

        (self.temp_line,) = self.ax_temp.plot([], [], color=TEMP_COLOR, linewidth=4.0)
        (self.power_line,) = self.ax_power.plot([], [], color=POWER_COLOR, linewidth=4.2, label="Average Power")

        self._power_unit = "W"
        self._power_key_pressed = False
        self._power_kw_formatter = FuncFormatter(lambda value, _: f"{value / 1000:.2f}")
        self._power_watt_formatter = self.ax_power.yaxis.get_major_formatter()
        self.bind("<KeyPress-k>", self._on_power_unit_key_press)
        self.bind("<KeyRelease-k>", self._on_power_unit_key_release)

        self.ax_temp.set_title("温度の推移", color=TEXT_PRIMARY, fontweight="bold", fontsize=30, pad=16)
        self.ax_power.set_title("直近1分間の平均消費電力", color=TEXT_PRIMARY, fontweight="bold", fontsize=30, pad=16,)

        canvas = FigureCanvasTkAgg(fig, master=parent)
        self.canvas_widget = canvas.get_tk_widget()
        self.canvas_widget.configure(bg=BG_COLOR, highlightthickness=0)
        self.canvas_widget.grid(row=0, column=0, sticky="nsew")
        self.canvas = canvas
        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    def _format_elapsed_time(self, value: float, _: int = 0) -> str:
        if not math.isfinite(value) or value < 0:
            return ""
        total_seconds = int(round(value))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    # ------------------------------------------------------------------
    def _build_setpoint_panel(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=PANEL_COLOR, bd=0, relief="flat", padx=20, pady=10)
        panel.grid(row=0, column=0, sticky="new", pady=(0, 18))
        panel.columnconfigure(0, weight=1)

        title = tk.Label(panel, text="設定温度", font=("Yu Gothic UI", 24, "bold"), fg=TEXT_PRIMARY, bg=PANEL_COLOR)
        title.grid(row=0, column=0, sticky="w")

        self.lbl_sv_value = tk.Label(
            panel,
            text="-- ℃",
            font=("Yu Gothic UI", 48, "bold"),
            fg=ACCENT_COLOR,
            bg=PANEL_COLOR,
        )
        self.lbl_sv_value.grid(row=1, column=0, sticky="w", pady=(0, 0))

    # ------------------------------------------------------------------
    def _build_showcase(self, parent: tk.Frame) -> None:
        # ★ ロゴは除外（右端に直接配置するため）
        sections = [
            ("デバイス1", DEVICE1_IMAGE_PATH, "熱風循環式エアヒーター\nLHS 410 SF-R"),
            ("デバイス2", DEVICE2_IMAGE_PATH, "熱風循環式高圧送風機\nチヌーク"),
        ]

        for idx, (_, path, caption) in enumerate(sections):
            frame = tk.Frame(parent, bg=PANEL_COLOR, padx=12, pady=16)
            frame.grid(row=idx + 1, column=0, sticky="nsew", pady=(0 if idx == 0 else 18, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)

            self._img_sources[idx] = self._load_image_pil(path)

            label = tk.Label(frame, bg=PANEL_COLOR)
            label.grid(row=0, column=0, sticky="nsew")
            self._img_labels[idx] = label

            caption_label = tk.Label(
                frame,
                text=caption,
                font=("Yu Gothic UI", 18, "bold"),
                fg=TEXT_PRIMARY,
                bg=PANEL_COLOR,
                justify="center",
            )
            caption_label.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            self._caption_labels[idx] = caption_label

            frame.bind("<Configure>", lambda event, section_idx=idx: self._update_section_image(section_idx))

        self.after(0, self._refresh_showcase_images)

    # ------------------------------------------------------------------
    def _load_image_pil(self, path: Optional[str]) -> Optional["Image.Image"]:
        if not path or not self._pil_available:
            return None
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            return None

    def _resize_image_keep_aspect(self, pil_img: "Image.Image", max_w: int, max_h: int) -> "ImageTk.PhotoImage":
        max_w = max(1, max_w)
        max_h = max(1, max_h)
        w, h = pil_img.size
        scale = min(max_w / w, max_h / h)  # contain
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        resized = pil_img.resize(new_size, Image.LANCZOS)
        return ImageTk.PhotoImage(resized)

    def _create_placeholder_image(self, width: int, height: int) -> tk.PhotoImage:
        width = max(1, width)
        height = max(1, height)
        image = tk.PhotoImage(width=width, height=height)
        for x in range(width):
            blend = x / max(1, width - 1)
            r = int(18 + (46 - 18) * blend)
            g = int(30 + (110 - 30) * blend)
            b = int(60 + (180 - 60) * blend)
            color = f"#{r:02x}{g:02x}{b:02x}"
            image.put(color, to=(x, 0, x + 1, height))
        overlay_color = "#1f2937"
        overlay_height = max(1, int(height * 0.2))
        image.put(overlay_color, to=(0, height - overlay_height, width, height))
        return image

    def _update_section_image(self, idx: int) -> None:
        label = self._img_labels.get(idx)
        if not label or not label.winfo_exists():
            return

        frame = label.master
        frame_width = max(1, frame.winfo_width() - 24)
        frame_height = max(1, frame.winfo_height() - 32)

        caption = self._caption_labels.get(idx)
        if caption and caption.winfo_ismapped():
            caption_height = max(caption.winfo_height(), caption.winfo_reqheight())
            frame_height = max(1, frame_height - caption_height - 12)

        pil_src = self._img_sources.get(idx)
        if pil_src is not None and self._pil_available:
            tk_img = self._resize_image_keep_aspect(pil_src, frame_width, frame_height)
        else:
            tk_img = self._create_placeholder_image(frame_width, frame_height)

        self._img_tk_cache[idx] = tk_img
        label.configure(image=tk_img)

    def _refresh_showcase_images(self) -> None:
        for idx in list(self._img_labels.keys()):
            self._update_section_image(idx)

    # === ロゴ（右端に直接配置） ==========================================
    def _update_logo_position(self, event: Optional[tk.Event] = None) -> None:
        """ウィンドウ右端にロゴを直貼り（横幅=ウィンドウの20%）。高さは比率で決定。"""
        total_w = max(1, self.winfo_width())
        total_h = max(1, self.winfo_height())

        logo_w = int(total_w * 0.20)  # 指定：横幅は20%
        if logo_w <= 0:
            return

        if self.logo_image_pil is not None and self._pil_available:
            # アスペクト維持で幅優先（contain: 幅はピッタリ、余った高さは余白）
            w, h = self.logo_image_pil.size
            scale = logo_w / max(1, w)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            resized = self.logo_image_pil.resize((new_w, new_h), Image.LANCZOS)
            tk_img = ImageTk.PhotoImage(resized)
            self._logo_tk_ref = tk_img
            self.logo_label.configure(image=tk_img)
            logo_h = new_h
        else:
            # Pillow 不在時はプレースホルダを幅20%・任意高さで生成（高さは右ペイン高さに近似）
            logo_h = int(total_h * 0.12)
            ph = self._create_placeholder_image(max(1, logo_w), max(1, logo_h))
            self._logo_tk_ref = ph
            self.logo_label.configure(image=ph)

        # 右下に配置（下端そろえ）。必要に応じて y 位置は調整可能。
        x = total_w - logo_w
        y = total_h - logo_h
        self.logo_label.place(x=x, y=y, width=logo_w, height=logo_h)

    # ------------------------------------------------------------------
    def _on_root_resize(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        # 右列を厳密に20%幅に
        total_w = max(1, self.winfo_width())
        target_right = int(total_w * self._right_ratio)
        self.grid_columnconfigure(1, weight=0, minsize=target_right)
        self.grid_columnconfigure(0, weight=1)
        # ロゴ位置/サイズも更新
        self._update_logo_position()

    def _on_right_frame_resize(self, event: tk.Event) -> None:
        if event.widget is not self.right_frame:
            return
        total_h = max(1, self.right_frame.winfo_height())
        for row, ratio in enumerate(self._section_ratios):
            minsize = int(total_h * ratio)
            self.right_frame.grid_rowconfigure(row, minsize=minsize, weight=1)

    # ------------------------------------------------------------------
    @staticmethod
    def _compute_average_power_w(
        times: List[dt.datetime],
        currents: List[float],
        now: dt.datetime,
        voltage_v: float,
        window_sec: float = 60.0,
    ) -> float:
        if not times or not currents or window_sec <= 0:
            return 0.0

        cutoff = now - dt.timedelta(seconds=window_sec)
        if times[-1] < cutoff:
            return 0.0

        pts = []
        n = len(times)

        start_idx = 0
        while start_idx < n and times[start_idx] < cutoff:
            start_idx += 1

        if start_idx == 0:
            pass
        elif start_idx < n:
            t_prev = times[start_idx - 1]
            i_prev = currents[start_idx - 1]
            t_next = times[start_idx]
            i_next = currents[start_idx]
            if t_next > t_prev:
                ratio = (cutoff - t_prev).total_seconds() / (t_next - t_prev).total_seconds()
                ratio = max(0.0, min(1.0, ratio))
                i_interp = i_prev + (i_next - i_prev) * ratio
                pts.append((cutoff, i_interp))
        else:
            pts.append((cutoff, currents[-1]))

        for idx in range(start_idx, n):
            if times[idx] >= cutoff:
                pts.append((times[idx], currents[idx]))

        if not pts:
            return 0.0

        if pts[0][0] > cutoff:
            pts.insert(0, (cutoff, pts[0][1]))
        if pts[-1][0] < now:
            pts.append((now, pts[-1][1]))

        total_current_seconds = 0.0
        for idx in range(len(pts) - 1):
            t0, i0 = pts[idx]
            t1, i1 = pts[idx + 1]
            if t1 <= t0:
                continue
            dt_seconds = (t1 - t0).total_seconds()
            avg_current = 0.5 * (i0 + i1)
            total_current_seconds += avg_current * dt_seconds

        average_current = total_current_seconds / window_sec
        return voltage_v * average_current

    # ------------------------------------------------------------------
    def poll_worker(self) -> None:
        while not self.stop_evt.is_set():
            cycle_start = time.perf_counter()
            now = dt.datetime.now()

            pv = {"value": None}
            sv = {"value": None}
            current_resp = {"value": None}

            try:
                with self.io_lock:
                    pv = self.cwf.read_e5cd_pv_decimal(node=E5CD_NODE, sid=SID)
                    sv = self.cwf.read_e5cd_sv_decimal(node=E5CD_NODE, sid=SID)
                    current_resp = self.cwf.read_g3pw_current_amps(node=CURRENT_NODE, sid=SID)
            except Exception as exc:  # デバッグログ
                print("[ERR] ポーリング失敗:", exc, file=sys.stderr)

            self.result_q.put((now, pv, sv, current_resp))

            spent = time.perf_counter() - cycle_start
            sleep_time = max(0.0, POLL_MS / 1000.0 - spent)
            if self.stop_evt.wait(timeout=sleep_time):
                break

    # ------------------------------------------------------------------
    def drain_results(self) -> None:
        try:
            while True:
                now, pv, sv, current_resp = self.result_q.get_nowait()

                if pv.get("value") is not None:
                    try:
                        pv_value = float(pv["value"])
                    except (TypeError, ValueError):
                        pv_value = None
                    if pv_value is not None:
                        self.temp_times.append(now)
                        self.temp_values.append(pv_value)

                if sv.get("value") is not None:
                    try:
                        sv_value = float(sv["value"])
                        sv_text = f"{sv_value:.1f} ℃"
                    except (TypeError, ValueError):
                        sv_text = f"{sv['value']}"
                    self.lbl_sv_value.config(text=sv_text)

                if current_resp.get("value") is not None:
                    try:
                        current_val = float(current_resp["value"])
                    except (TypeError, ValueError):
                        current_val = None
                    if current_val is not None:
                        self.current_times.append(now)
                        self.currents.append(current_val)

                        avg_power_w = self._compute_average_power_w(
                            self.current_times,
                            self.currents,
                            now,
                            voltage_v=VOLTAGE_V,
                            window_sec=60.0,
                        )

                        self.power_times.append(now)
                        self.power_values.append(avg_power_w)

                        cutoff = now - dt.timedelta(seconds=120)
                        while self.current_times and self.current_times[0] < cutoff:
                            self.current_times.pop(0)
                            self.currents.pop(0)

                elapsed_temp: List[float] = []
                elapsed_power: List[float] = []

                if self.temp_times:
                    elapsed_temp = [
                        (ts - self.t0).total_seconds() for ts in self.temp_times
                    ]
                    self.temp_line.set_data(elapsed_temp, self.temp_values)
                    t_min = min(self.temp_values)
                    t_max = max(self.temp_values)
                    if t_min == t_max:
                        t_min -= 1.0
                        t_max += 1.0
                    padding = max(1.0, (t_max - t_min) * 0.15)
                    self.ax_temp.set_ylim(t_min - padding, t_max + padding)

                if self.power_times:
                    elapsed_power = [
                        (ts - self.t0).total_seconds() for ts in self.power_times
                    ]
                    self.power_line.set_data(elapsed_power, self.power_values)
                    p_min = min(self.power_values)
                    p_max = max(self.power_values)
                    if p_min == p_max:
                        pad = max(0.05, p_max * 0.1 if p_max else 0.1)
                        p_min -= pad
                        p_max += pad
                    padding = max(0.05, (p_max - p_min) * 0.15)
                    self.ax_power.set_ylim(p_min - padding, p_max + padding)

                if elapsed_temp or elapsed_power:
                    x_candidates = [1.0]
                    if elapsed_temp:
                        x_candidates.append(elapsed_temp[-1])
                    if elapsed_power:
                        x_candidates.append(elapsed_power[-1])
                    x_max = max(x_candidates)
                    if elapsed_temp:
                        self.ax_temp.set_xlim(0.0, x_max)
                    if elapsed_power:
                        self.ax_power.set_xlim(0.0, x_max)

                self.canvas.draw_idle()
        except queue.Empty:
            pass

        if not self.stop_evt.is_set():
            self.after(120, self.drain_results)

    # ------------------------------------------------------------------
    def _on_power_unit_key_press(self, event: tk.Event) -> None:
        if getattr(event, "keysym", "").lower() != "k":
            return
        if self._power_key_pressed:
            return
        self._power_key_pressed = True
        self._toggle_power_axis_units()

    def _on_power_unit_key_release(self, event: tk.Event) -> None:
        if getattr(event, "keysym", "").lower() == "k":
            self._power_key_pressed = False

    def _toggle_power_axis_units(self) -> None:
        self._power_unit = "kW" if self._power_unit == "W" else "W"
        if self._power_unit == "kW":
            self.ax_power.set_ylabel("平均消費電力 [kW]", color=TEXT_PRIMARY, labelpad=20)
            self.ax_power.yaxis.set_major_formatter(self._power_kw_formatter)
        else:
            self.ax_power.set_ylabel("平均消費電力 [W]", color=TEXT_PRIMARY, labelpad=20)
            self.ax_power.yaxis.set_major_formatter(self._power_watt_formatter)

        self.ax_power.relim()
        self.ax_power.autoscale_view()
        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        try:
            self.stop_evt.set()
            if hasattr(self, "worker") and self.worker is not None and self.worker.is_alive():
                self.worker.join(timeout=1.5)
            if hasattr(self, "cwf") and self.cwf:
                try:
                    self.cwf.close()
                except Exception:
                    pass
        finally:
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
