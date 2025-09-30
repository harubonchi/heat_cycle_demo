#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import datetime as dt
import threading
import queue
import tkinter as tk
from tkinter import messagebox
import tkinter.font as tkfont

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.dates import DateFormatter

from compowayf_driver import CompoWayFDriver

# ==== シリアル・計測設定 ====
PORT = "/dev/ttyUSB0"
E5CD_NODE = "01"
SID = "0"
POLL_MS = 0  # 0なら応答を受け次第すぐに次コマンドを送る
VOLTAGE_V = 200.0  # 指示に従い電圧は固定

# 電力計測対象システム（必要に応じて node を変更）
POWER_SYSTEMS = [
    {"id": "system_a", "label": "システムA", "node": "02", "color": "#1f6feb"},
    {"id": "system_b", "label": "システムB", "node": "03", "color": "#60a5fa"},
]

# ---- 表示設定 ----
BG_COLOR = "#0b1220"
CARD_COLOR = "#111c34"
ACCENT_COLOR = "#38bdf8"
TEXT_COLOR = "#f8fafc"
GRID_COLOR = "#1f2a44"
TEMP_COLOR = "#f87171"
TEMP_LINEWIDTH = 3.0
POWER_LINEWIDTH = 3.2
POWER_STABLE_LINEWIDTH = 2.4
LOGO_IMAGE_PATH = None  # 実際のロゴ画像に差し替える場合はファイルパスを指定

# 安定判定パラメータ
STABLE_MIN_DURATION_SEC = 60.0
STABLE_TOLERANCE_WH = 0.05  # 安定とみなす許容幅（必要に応じて調整）


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("リアルタイム温度・電力モニター")
        self.configure(bg=BG_COLOR)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Tk全体のフォントを大きめに
        try:
            default_font = tkfont.nametofont("TkDefaultFont")
            default_font.configure(family="Yu Gothic UI", size=16)
        except tk.TclError:
            pass

        # データ保持
        self.t0 = dt.datetime.now()
        self.temp_times = []
        self.temp_values = []
        self.system_series = {}
        for system in POWER_SYSTEMS:
            self.system_series[system["id"]] = {
                "times_current": [],
                "currents": [],
                "times_power": [],
                "power_wh": [],
                "stable_avg": None,
                "line": None,
                "stable_line": None,
                "reference_text": None,
            }
        self.sv_value = None

        # ドライバ初期化
        try:
            self.cwf = CompoWayFDriver(port=PORT)
        except Exception as exc:
            messagebox.showerror("シリアル接続エラー", str(exc))
            self.destroy()
            sys.exit(1)

        self.io_lock = threading.Lock()

        # レイアウト構築
        self.columnconfigure(0, weight=4)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        graph_frame = tk.Frame(self, bg=BG_COLOR)
        graph_frame.grid(row=0, column=0, sticky="nsew", padx=(24, 12), pady=24)

        info_frame = tk.Frame(self, bg=BG_COLOR)
        info_frame.grid(row=0, column=1, sticky="nsew", padx=(12, 24), pady=24)
        info_frame.rowconfigure(0, weight=0)
        info_frame.rowconfigure(1, weight=1)

        self._build_info_panel(info_frame)
        self._build_figure(graph_frame)

        # 通信結果キュー
        self.result_q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = threading.Thread(target=self.poll_worker, daemon=True)
        self.worker.start()

        self.after(100, self.drain_results)

    # ------------------------------------------------------------------
    def _build_info_panel(self, parent: tk.Frame):
        card = tk.Frame(parent, bg=CARD_COLOR, bd=0, relief="flat", padx=20, pady=20)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)

        title = tk.Label(
            card,
            text="設定温度",
            font=("Yu Gothic UI", 20, "bold"),
            fg=TEXT_COLOR,
            bg=CARD_COLOR,
        )
        title.grid(row=0, column=0, sticky="w")

        self.lbl_sv_value = tk.Label(
            card,
            text="-- ℃",
            font=("Yu Gothic UI", 40, "bold"),
            fg=ACCENT_COLOR,
            bg=CARD_COLOR,
        )
        self.lbl_sv_value.grid(row=1, column=0, sticky="w", pady=(12, 0))

        subtitle = tk.Label(
            card,
            text="制御装置から取得した最新の設定値",
            font=("Yu Gothic UI", 16),
            fg="#94a3b8",
            bg=CARD_COLOR,
        )
        subtitle.grid(row=2, column=0, sticky="w", pady=(8, 0))

        logo_card = tk.Frame(parent, bg=CARD_COLOR, bd=0, relief="flat", padx=20, pady=20)
        logo_card.grid(row=1, column=0, sticky="nsew", pady=(24, 0))
        logo_card.columnconfigure(0, weight=1)
        logo_card.rowconfigure(0, weight=0)
        logo_card.rowconfigure(1, weight=1)

        logo_title = tk.Label(
            logo_card,
            text="企業ロゴ表示エリア",
            font=("Yu Gothic UI", 18, "bold"),
            fg=TEXT_COLOR,
            bg=CARD_COLOR,
        )
        logo_title.grid(row=0, column=0, sticky="nw")

        self.logo_image = self._load_logo_image()
        self.logo_label = tk.Label(
            logo_card,
            image=self.logo_image,
            text="ロゴをここに配置",
            compound="center",
            font=("Yu Gothic UI", 20, "bold"),
            fg=TEXT_COLOR,
            bg=CARD_COLOR,
        )
        self.logo_label.grid(row=1, column=0, sticky="nsew", pady=(12, 0))

    def _build_figure(self, parent: tk.Frame):
        fig = Figure(figsize=(12, 7), dpi=100)
        fig.patch.set_facecolor(BG_COLOR)
        self.ax_temp = fig.add_subplot(211)
        self.ax_power = fig.add_subplot(212, sharex=self.ax_temp)
        fig.subplots_adjust(left=0.08, right=0.88, top=0.95, bottom=0.08, hspace=0.28)

        for ax in (self.ax_temp, self.ax_power):
            ax.set_facecolor("#0f1a2f")
            ax.tick_params(axis="x", colors=TEXT_COLOR, labelsize=12)
            ax.tick_params(axis="y", colors=TEXT_COLOR, labelsize=12)
            for spine in ax.spines.values():
                spine.set_color("#1e293b")
            ax.grid(True, color=GRID_COLOR, alpha=0.55, linewidth=1.0)

        self.ax_temp.set_title("温度の推移", fontsize=20, color=TEXT_COLOR, fontweight="bold", pad=14)
        self.ax_temp.set_ylabel("温度 (℃)", fontsize=16, color=TEXT_COLOR, labelpad=12)
        self.ax_temp.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))
        self.ax_temp.tick_params(axis="x", which="both", labelbottom=False)

        self.ax_power.set_title("消費電力量の推移（直近1分相当）", fontsize=20, color=TEXT_COLOR, fontweight="bold", pad=14)
        self.ax_power.set_ylabel("電力量 (Wh)", fontsize=16, color=TEXT_COLOR, labelpad=12)
        self.ax_power.set_xlabel("時刻", fontsize=16, color=TEXT_COLOR, labelpad=12)
        self.ax_power.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))

        (self.temp_line,) = self.ax_temp.plot([], [], color=TEMP_COLOR, linewidth=TEMP_LINEWIDTH)

        legend_handles = []
        for idx, system in enumerate(POWER_SYSTEMS):
            line, = self.ax_power.plot([], [], color=system["color"], linewidth=POWER_LINEWIDTH, label=system["label"])
            stable_line = self.ax_power.axhline(
                y=0.0,
                color=system["color"],
                linewidth=POWER_STABLE_LINEWIDTH,
                linestyle="--",
                alpha=0.75,
                visible=False,
            )
            ref_text = self.ax_power.text(
                1.02,
                0.9 - idx * 0.12,
                f"{system['label']} 安定値: ---",
                transform=self.ax_power.transAxes,
                fontsize=14,
                fontweight="bold",
                color=system["color"],
                ha="left",
                va="center",
                clip_on=False,
            )
            self.system_series[system["id"]]["line"] = line
            self.system_series[system["id"]]["stable_line"] = stable_line
            self.system_series[system["id"]]["reference_text"] = ref_text
            legend_handles.append(line)

        legend = self.ax_power.legend(
            handles=legend_handles,
            loc="upper left",
            facecolor="#132041",
            framealpha=0.9,
            edgecolor="#1e293b",
            fontsize=12,
        )
        for text in legend.get_texts():
            text.set_color(TEXT_COLOR)

        self.canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas_widget = self.canvas.get_tk_widget()
        canvas_widget.configure(bg=BG_COLOR, highlightthickness=0)
        canvas_widget.pack(fill=tk.BOTH, expand=True)
        self.canvas.draw_idle()

    def _load_logo_image(self):
        if LOGO_IMAGE_PATH:
            try:
                return tk.PhotoImage(file=LOGO_IMAGE_PATH)
            except tk.TclError:
                pass
        width, height = 360, 180
        img = tk.PhotoImage(width=width, height=height)
        for x in range(width):
            blend = x / max(1, width - 1)
            r = int(17 + (56 - 17) * blend)
            g = int(28 + (130 - 28) * blend)
            b = int(52 + (190 - 52) * blend)
            color = f"#{r:02x}{g:02x}{b:02x}"
            img.put(color, to=(x, 0, x + 1, height))
        return img

    # ------------------------------------------------------------------
    @staticmethod
    def _integrate_power_wh(times, currents, now, voltage_v, window_sec=60.0):
        if not times or not currents:
            return 0.0
        t_start = now - dt.timedelta(seconds=window_sec)

        n = len(times)
        idx = n - 1
        while idx > 0 and times[idx - 1] >= t_start:
            idx -= 1

        pts = []
        if idx > 0 and times[idx - 1] <= t_start <= times[idx]:
            t0, i0 = times[idx - 1], currents[idx - 1]
            t1, i1 = times[idx], currents[idx]
            if t1 > t0:
                span = (t1 - t0).total_seconds()
                ratio = (t_start - t0).total_seconds() / span
                ratio = max(0.0, min(1.0, ratio))
                i_interp = i0 + (i1 - i0) * ratio
                pts.append((t_start, i_interp))
        for pos in range(idx, n):
            if times[pos] >= t_start:
                pts.append((times[pos], currents[pos]))
        if len(pts) < 2:
            return 0.0

        energy_wh = 0.0
        for a in range(len(pts) - 1):
            t0, i0 = pts[a]
            t1, i1 = pts[a + 1]
            if t1 <= t0:
                continue
            dt_seconds = (t1 - t0).total_seconds()
            avg_current = 0.5 * (i0 + i1)
            power_w = voltage_v * avg_current
            energy_wh += power_w * (dt_seconds / 3600.0)
        return energy_wh

    @staticmethod
    def _time_weighted_average(times, values):
        if not times or not values:
            return None
        if len(times) == 1:
            return values[0]
        total = 0.0
        duration = 0.0
        for idx in range(len(times) - 1):
            t0, t1 = times[idx], times[idx + 1]
            if t1 <= t0:
                continue
            dt_seconds = (t1 - t0).total_seconds()
            avg_value = 0.5 * (values[idx] + values[idx + 1])
            total += avg_value * dt_seconds
            duration += dt_seconds
        return total / duration if duration > 0 else values[-1]

    def _evaluate_stability(self, system_id):
        series = self.system_series[system_id]
        times = series["times_power"]
        values = series["power_wh"]
        if len(times) < 2:
            series["stable_avg"] = None
            return
        newest_time = times[-1]
        cutoff = newest_time - dt.timedelta(seconds=STABLE_MIN_DURATION_SEC)
        start_idx = 0
        for i, t in enumerate(times):
            if t >= cutoff:
                start_idx = i
                break
        window_times = times[start_idx:]
        window_values = values[start_idx:]
        if not window_times:
            series["stable_avg"] = None
            return
        if (window_times[-1] - window_times[0]).total_seconds() < STABLE_MIN_DURATION_SEC:
            series["stable_avg"] = None
            return
        vmax = max(window_values)
        vmin = min(window_values)
        if vmax - vmin <= STABLE_TOLERANCE_WH:
            avg = self._time_weighted_average(window_times, window_values)
            series["stable_avg"] = avg
        else:
            series["stable_avg"] = None

    # ------------------------------------------------------------------
    def poll_worker(self):
        while not self.stop_evt.is_set():
            cycle_start = time.perf_counter()
            now = dt.datetime.now()

            pv = {"value": None}
            sv = {"value": None}
            currents = {}
            try:
                with self.io_lock:
                    pv = self.cwf.read_e5cd_pv_decimal(node=E5CD_NODE, sid=SID)
                    sv = self.cwf.read_e5cd_sv_decimal(node=E5CD_NODE, sid=SID)
                    for system in POWER_SYSTEMS:
                        try:
                            cur = self.cwf.read_g3pw_current_amps(node=system["node"], sid=SID)
                        except Exception as current_exc:
                            print(f"[ERR] current read {system['id']}: {current_exc}", file=sys.stderr)
                            cur = {"value": None}
                        currents[system["id"]] = cur
            except Exception as exc:
                print("[ERR] poll I/O:", exc, file=sys.stderr)

            self.result_q.put((now, pv, sv, currents))

            spent = time.perf_counter() - cycle_start
            sleep_time = max(0.0, POLL_MS / 1000.0 - spent)
            if self.stop_evt.wait(timeout=sleep_time):
                break

    # ------------------------------------------------------------------
    def drain_results(self):
        try:
            while True:
                now, pv, sv, currents = self.result_q.get_nowait()

                if pv.get("value") is not None:
                    try:
                        value = float(pv["value"])
                    except (TypeError, ValueError):
                        value = None
                    if value is not None:
                        self.temp_times.append(now)
                        self.temp_values.append(value)

                if sv.get("value") is not None:
                    try:
                        sv_value_float = float(sv["value"])
                        sv_text = f"{sv_value_float:.1f} ℃"
                    except (TypeError, ValueError):
                        sv_text = f"{sv['value']}"
                    self.sv_value = sv_text
                    self.lbl_sv_value.config(text=sv_text)

                for system in POWER_SYSTEMS:
                    resp = currents.get(system["id"], {})
                    if resp.get("value") is None:
                        continue
                    try:
                        current_val = float(resp["value"])
                    except (TypeError, ValueError):
                        continue
                    series = self.system_series[system["id"]]
                    series["times_current"].append(now)
                    series["currents"].append(current_val)
                    energy_wh = self._integrate_power_wh(
                        series["times_current"],
                        series["currents"],
                        now,
                        voltage_v=VOLTAGE_V,
                        window_sec=60.0,
                    )
                    series["times_power"].append(now)
                    series["power_wh"].append(energy_wh)
                    self._evaluate_stability(system["id"])

                if self.temp_times:
                    self.temp_line.set_data(self.temp_times, self.temp_values)
                    self.ax_temp.set_xlim(self.t0, now)
                    temp_min = min(self.temp_values)
                    temp_max = max(self.temp_values)
                    if temp_min == temp_max:
                        temp_min -= 1.0
                        temp_max += 1.0
                    padding = max(1.0, (temp_max - temp_min) * 0.1)
                    self.ax_temp.set_ylim(temp_min - padding, temp_max + padding)

                power_values = []
                for system in POWER_SYSTEMS:
                    series = self.system_series[system["id"]]
                    if not series["times_power"]:
                        series["line"].set_data([], [])
                        series["stable_line"].set_visible(False)
                        series["reference_text"].set_text(f"{system['label']} 安定値: ---")
                        continue
                    series["line"].set_data(series["times_power"], series["power_wh"])
                    power_values.extend(series["power_wh"])

                    stable_avg = series["stable_avg"]
                    if stable_avg is not None:
                        series["stable_line"].set_ydata([stable_avg, stable_avg])
                        series["stable_line"].set_visible(True)
                        series["reference_text"].set_text(f"{system['label']} 安定値: {stable_avg:.3f} Wh")
                    else:
                        series["stable_line"].set_visible(False)
                        series["reference_text"].set_text(f"{system['label']} 安定値: ---")

                if power_values:
                    self.ax_power.set_xlim(self.t0, now)
                    p_min = min(power_values)
                    p_max = max(power_values)
                    if p_min == p_max:
                        pad = max(0.05, p_max * 0.1 if p_max != 0 else 0.1)
                        p_min -= pad
                        p_max += pad
                    padding = max(0.05, (p_max - p_min) * 0.1)
                    self.ax_power.set_ylim(p_min - padding, p_max + padding)

                self.canvas.draw_idle()
        except queue.Empty:
            pass

        if not self.stop_evt.is_set():
            self.after(120, self.drain_results)

    # ------------------------------------------------------------------
    def on_close(self):
        try:
            self.stop_evt.set()
            if hasattr(self, "worker") and self.worker.is_alive():
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
