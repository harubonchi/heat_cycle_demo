#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""リアルタイム温度・電力モニター GUI"""

import sys
import os
import time
import datetime as dt
import math
import threading
import queue
import tkinter as tk
from tkinter import messagebox
import tkinter.font as tkfont
from fractions import Fraction
from typing import List, Optional

import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.dates import DateFormatter

import japanize_matplotlib  # noqa: F401  # フォントを日本語対応させる

from compowayf_driver import CompoWayFDriver


# ==== シリアル・計測設定 ====
PORT = "/dev/ttyUSB0"
E5CD_NODE = "01"
CURRENT_NODE = "02"
SID = "0"
POLL_MS = 0  # 応答を受け次第次コマンドを送信
VOLTAGE_V = 200.0  # 指定通り電圧は固定


# ---- デザイン設定 ----
BG_COLOR = "#060b16"
PANEL_COLOR = "#0e1626"
ACCENT_COLOR = "#38bdf8"
TEXT_PRIMARY = "#f8fafc"
TEXT_SECONDARY = "#94a3b8"
GRID_COLOR = "#1f2a44"
TEMP_COLOR = "#ef4444"
POWER_COLOR = "#2563eb"


# ---- 画像プレースホルダ設定 ----
script_dir = os.path.dirname(os.path.abspath(__file__))
DEVICE1_IMAGE_PATH = os.path.join(script_dir, "product_1.png")
DEVICE2_IMAGE_PATH = os.path.join(script_dir, "product_2.png")
LOGO_IMAGE_PATH = os.path.join(script_dir, "Leister_Logo.png")


class App(tk.Tk):
    """温度・電力量を表示する Tkinter アプリ"""

    def __init__(self) -> None:
        super().__init__()
        self.title("リアルタイム温度・電力モニター")
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
        self.temp_times = []
        self.temp_values = []
        self.current_times = []
        self.currents = []
        self.power_times = []
        self.power_values = []

        try:
            self.cwf = CompoWayFDriver(port=PORT)
        except Exception as exc:  # pragma: no cover - GUI メッセージ用
            messagebox.showerror("シリアル接続エラー", str(exc))
            self.destroy()
            sys.exit(1)

        self.io_lock = threading.Lock()

        self.columnconfigure(0, weight=5)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left_frame = tk.Frame(self, bg=BG_COLOR)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(16, 6), pady=16)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)

        right_frame = tk.Frame(self, bg=BG_COLOR)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 16), pady=16)
        right_frame.columnconfigure(0, weight=1)
        right_frame.rowconfigure(0, weight=0)
        right_frame.rowconfigure(1, weight=4)
        right_frame.rowconfigure(2, weight=4)
        right_frame.rowconfigure(3, weight=2)

        self._build_graph_area(left_frame)
        self._build_setpoint_panel(right_frame)
        self._build_showcase(right_frame)

        self.result_q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = threading.Thread(target=self.poll_worker, daemon=True)
        self.worker.start()

        self.after(100, self.drain_results)

    # ------------------------------------------------------------------
    def _build_graph_area(self, parent: tk.Frame) -> None:
        fig = Figure(figsize=(12.4, 8.2), dpi=100)
        fig.patch.set_facecolor(BG_COLOR)
        gs = fig.add_gridspec(2, 1, hspace=0.32)
        self.ax_temp = fig.add_subplot(gs[0])
        self.ax_power = fig.add_subplot(gs[1], sharex=self.ax_temp)
        fig.subplots_adjust(left=0.06, right=0.99, top=0.94, bottom=0.12)

        for ax in (self.ax_temp, self.ax_power):
            ax.set_facecolor(PANEL_COLOR)
            ax.tick_params(axis="x", colors=TEXT_PRIMARY, labelsize=16, width=1.8, length=8, pad=10)
            ax.tick_params(axis="y", colors=TEXT_PRIMARY, labelsize=16, width=1.8, length=8, pad=10)
            for spine in ax.spines.values():
                spine.set_color("#1e293b")
            ax.grid(True, color=GRID_COLOR, alpha=0.55, linewidth=1.2)

        self.ax_temp.set_ylabel("温度 (℃)", color=TEXT_PRIMARY, labelpad=18)
        self.ax_temp.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))
        self.ax_temp.tick_params(axis="x", which="both", labelbottom=False)

        self.ax_power.set_ylabel("電力量 (Wh)", color=TEXT_PRIMARY, labelpad=20)
        self.ax_power.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))

        (self.temp_line,) = self.ax_temp.plot([], [], color=TEMP_COLOR, linewidth=4.0)
        (self.power_line,) = self.ax_power.plot([], [], color=POWER_COLOR, linewidth=4.2, label="消費電力量")

        self.ax_temp.set_title(
            "温度の推移",
            color=TEXT_PRIMARY,
            fontweight="bold",
            fontsize=24,
            pad=16,
        )
        self.ax_power.set_title(
            "直近1分相当の消費電力量",
            color=TEXT_PRIMARY,
            fontweight="bold",
            fontsize=24,
            pad=16,
        )

        canvas = FigureCanvasTkAgg(fig, master=parent)
        self.canvas_widget = canvas.get_tk_widget()
        self.canvas_widget.configure(bg=BG_COLOR, highlightthickness=0)
        self.canvas_widget.grid(row=0, column=0, sticky="nsew")
        self.canvas = canvas
        self.canvas.draw_idle()

    def _build_setpoint_panel(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=PANEL_COLOR, bd=0, relief="flat", padx=20, pady=24)
        panel.grid(row=0, column=0, sticky="new", pady=(0, 18))
        panel.columnconfigure(0, weight=1)

        title = tk.Label(
            panel,
            text="設定温度",
            font=("Yu Gothic UI", 24, "bold"),
            fg=TEXT_PRIMARY,
            bg=PANEL_COLOR,
        )
        title.grid(row=0, column=0, sticky="w")

        self.lbl_sv_value = tk.Label(
            panel,
            text="-- ℃",
            font=("Yu Gothic UI", 48, "bold"),
            fg=ACCENT_COLOR,
            bg=PANEL_COLOR,
        )
        self.lbl_sv_value.grid(row=1, column=0, sticky="w", pady=(18, 6))

    def _build_showcase(self, parent: tk.Frame) -> None:
        sections = [
            ("デバイス1", DEVICE1_IMAGE_PATH, "熱風循環式エアヒーター\nLHS 410 SF-R"),
            ("デバイス2", DEVICE2_IMAGE_PATH, "熱風循環式高圧送風機\nチヌーク"),
            ("ロゴ", LOGO_IMAGE_PATH, None),
        ]

        for idx, (_, path, caption) in enumerate(sections):
            frame = tk.Frame(parent, bg=PANEL_COLOR, padx=12, pady=16)
            frame.grid(row=idx + 1, column=0, sticky="nsew", pady=(0 if idx == 0 else 18, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)

            if idx == 0:
                img_width, img_height = 372, 284
            elif idx == 1:
                img_width, img_height = 352, 242
            else:
                img_width, img_height = 312, 160

            image = self._load_image(path, width=img_width, height=img_height)
            label = tk.Label(frame, image=image, bg=PANEL_COLOR)
            label.image = image
            label.grid(row=0, column=0, sticky="nsew")

            if caption:
                caption_label = tk.Label(
                    frame,
                    text=caption,
                    font=("Yu Gothic UI", 18, "bold" if idx < 2 else "normal"),
                    fg=TEXT_PRIMARY,
                    bg=PANEL_COLOR,
                    justify="center",
                )
                caption_label.grid(row=1, column=0, sticky="ew", pady=(12, 0))

    def _load_image(self, path: Optional[str], width: int, height: int) -> tk.PhotoImage:
        if path:
            try:
                image = tk.PhotoImage(file=path)
                img_width = image.width() or 1
                img_height = image.height() or 1

                needs_resize = img_width > width or img_height > height
                if needs_resize:
                    scale = min(width / img_width, height / img_height)
                    scale = max(scale, 0.0)
                    if scale < 1.0:
                        frac = Fraction(scale).limit_denominator(100)
                        numerator = max(1, frac.numerator)
                        denominator = max(1, frac.denominator)

                        if numerator < denominator:
                            image = image.zoom(numerator)
                            image = image.subsample(denominator)
                        else:
                            shrink_factor = max(2, math.ceil(img_width / width), math.ceil(img_height / height))
                            image = image.subsample(shrink_factor)

                        final_width = image.width() or 1
                        final_height = image.height() or 1
                        adjust_factor = max(
                            1,
                            math.ceil(final_width / width),
                            math.ceil(final_height / height),
                        )
                        if adjust_factor > 1:
                            image = image.subsample(adjust_factor)

                return image
            except tk.TclError:
                pass

        image = tk.PhotoImage(width=width, height=height)
        for x in range(width):
            blend = x / max(1, width - 1)
            r = int(18 + (46 - 18) * blend)
            g = int(30 + (110 - 30) * blend)
            b = int(60 + (180 - 60) * blend)
            color = f"#{r:02x}{g:02x}{b:02x}"
            image.put(color, to=(x, 0, x + 1, height))
        overlay_color = "#1f2937"
        image.put(overlay_color, to=(0, height - 48, width, height))
        return image

    # ------------------------------------------------------------------
    @staticmethod
    def _integrate_power_wh(
        times: List[dt.datetime],
        currents: List[float],
        now: dt.datetime,
        voltage_v: float,
        window_sec: float = 60.0,
    ) -> float:
        if not times or not currents:
            return 0.0

        cutoff = now - dt.timedelta(seconds=window_sec)

        if times[-1] < cutoff:
            return 0.0

        pts = []
        n = len(times)

        # 直近 window 内のデータ列を構築
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
            # 全てのサンプルが cutoff より前だが最後の値で延長
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

        energy_wh = 0.0
        for idx in range(len(pts) - 1):
            t0, i0 = pts[idx]
            t1, i1 = pts[idx + 1]
            if t1 <= t0:
                continue
            dt_seconds = (t1 - t0).total_seconds()
            avg_current = 0.5 * (i0 + i1)
            power_w = voltage_v * avg_current
            energy_wh += power_w * (dt_seconds / 3600.0)

        return energy_wh

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
            except Exception as exc:  # pragma: no cover - デバッグログ
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

                        energy_wh = self._integrate_power_wh(
                            self.current_times,
                            self.currents,
                            now,
                            voltage_v=VOLTAGE_V,
                            window_sec=60.0,
                        )

                        self.power_times.append(now)
                        self.power_values.append(energy_wh)

                        cutoff = now - dt.timedelta(seconds=120)
                        while self.current_times and self.current_times[0] < cutoff:
                            self.current_times.pop(0)
                            self.currents.pop(0)

                if self.temp_times:
                    self.temp_line.set_data(self.temp_times, self.temp_values)
                    self.ax_temp.set_xlim(self.t0, now)
                    t_min = min(self.temp_values)
                    t_max = max(self.temp_values)
                    if t_min == t_max:
                        t_min -= 1.0
                        t_max += 1.0
                    padding = max(1.0, (t_max - t_min) * 0.15)
                    self.ax_temp.set_ylim(t_min - padding, t_max + padding)

                if self.power_times:
                    self.power_line.set_data(self.power_times, self.power_values)
                    self.ax_power.set_xlim(self.t0, now)
                    p_min = min(self.power_values)
                    p_max = max(self.power_values)
                    if p_min == p_max:
                        pad = max(0.05, p_max * 0.1 if p_max else 0.1)
                        p_min -= pad
                        p_max += pad
                    padding = max(0.05, (p_max - p_min) * 0.15)
                    self.ax_power.set_ylim(p_min - padding, p_max + padding)

                self.canvas.draw_idle()
        except queue.Empty:
            pass

        if not self.stop_evt.is_set():
            self.after(120, self.drain_results)

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        try:
            self.stop_evt.set()
            if hasattr(self, "worker") and self.worker.is_alive():
                self.worker.join(timeout=1.5)
            if hasattr(self, "cwf") and self.cwf:
                try:
                    self.cwf.close()
                except Exception:  # pragma: no cover - 終了処理
                    pass
        finally:
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
