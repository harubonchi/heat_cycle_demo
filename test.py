#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import datetime as dt
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.dates import DateFormatter

from compowayf_driver import CompoWayFDriver

# ==== 設定 ====
PORT = "/dev/ttyUSB0"
E5CD_NODE = "01"
G3PW_NODE = "02"
SID = "0"
POLL_MS = 0              # 0なら可能な限り高速。固定周期にしたいなら 300 など
VOLTAGE_V = 200.0        # ★ 電圧[Volt]（可変にしたい場合はここを変更）

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Temperature / Setpoint / Energy Monitor (CompoWay/F)")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # データ保持
        self.t0 = dt.datetime.now()
        self.times_temp, self.vals_temp = [], []
        self.times_i,  self.vals_i  = [], []   # 電流(A)の時系列（生データ保持）
        self.times_energy, self.vals_energy = [], []  # 直近1分の消費電力量[Wh]の推移
        self.sv_value = None

        # ドライバ
        try:
            self.cwf = CompoWayFDriver(port=PORT)
        except Exception as e:
            messagebox.showerror("Serial Open Error", str(e))
            self.destroy()
            sys.exit(1)

        # シリアルの排他（同時送信を防ぐ）
        self.io_lock = threading.Lock()

        # ===== UI =====
        top = ttk.Frame(self, padding=8); top.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(top, text="SV (Set Value, °C):", font=("TkDefaultFont", 12, "bold")).pack(side=tk.LEFT)
        self.lbl_sv = ttk.Label(top, text="-- °C", font=("TkDefaultFont", 12))
        self.lbl_sv.pack(side=tk.LEFT, padx=(8, 0))

        fig = Figure(figsize=(8, 5), dpi=100)
        self.ax_temp = fig.add_subplot(211)
        self.ax_energy = fig.add_subplot(212)

        self.ax_temp.set_title("Temperature trend")
        self.ax_temp.set_xlabel("Time")
        self.ax_temp.set_ylabel("Temperature (°C)")
        self.ax_temp.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))

        self.ax_energy.set_title("Energy consumption (last 1 min)")
        self.ax_energy.set_xlabel("Time")
        self.ax_energy.set_ylabel("Energy (Wh)")
        self.ax_energy.xaxis.set_major_formatter(DateFormatter("%H:%M:%S"))

        (self.line_temp,) = self.ax_temp.plot([], [], linewidth=1.5)
        (self.line_energy,) = self.ax_energy.plot([], [], linewidth=1.5)

        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # バックグラウンド I/O 用
        self.result_q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = threading.Thread(target=self.poll_worker, daemon=True)
        self.worker.start()

        # UI 更新タイマー
        self.after(100, self.drain_results)

    # ---- ユーティリティ：直近1分のエネルギー[Wh]を台形則で積分 ----
    @staticmethod
    def _integrate_energy_wh(times, currents, now, voltage_v, window_sec=60.0):
        """
        times: [datetime,...], currents: [A,...]（同じ長さ、単調増加想定）
        区間 [now - window_sec, now] のエネルギーを台形則で求める（Wh）
        力率=1 を仮定し P[W] = V * I。E[Wh] = ∫ P dt / 3600
        """
        if not times or not currents:
            return None
        t_start = now - dt.timedelta(seconds=window_sec)

        # 1分より古い点は無視（ただし境界の内挿のため t_start 直前の1点は参照）
        n = len(times)
        idx = n - 1
        while idx >= 0 and times[idx] >= t_start:
            idx -= 1

        pts = []
        # 境界内挿
        if idx >= 0 and idx + 1 < n:
            t0, i0 = times[idx], currents[idx]
            t1, i1 = times[idx + 1], currents[idx + 1]
            if t1 > t0 and t1 >= t_start:
                dt_total = (t1 - t0).total_seconds()
                dt_part  = (t_start - t0).total_seconds()
                alpha = max(0.0, min(1.0, dt_part / dt_total))
                i_start = i0 + (i1 - i0) * alpha
                pts.append((t_start, i_start))

        # 窓内の生データ
        for k in range(max(idx + 1, 0), n):
            if times[k] >= t_start and times[k] <= now:
                pts.append((times[k], currents[k]))

        if len(pts) <= 1:
            return 0.0

        e_Wh = 0.0
        for a in range(len(pts) - 1):
            t0, i0 = pts[a]
            t1, i1 = pts[a + 1]
            if t1 <= t0:
                continue
            dt_s = (t1 - t0).total_seconds()
            i_avg = 0.5 * (i0 + i1)
            p_W = voltage_v * i_avg
            e_Wh += p_W * (dt_s / 3600.0)

        return e_Wh

    # ---- バックグラウンド：シリアルI/O ----
    def poll_worker(self):
        """一定周期で 3 コマンドを（ロック下で）順次実行し、結果をキューへ。"""
        while not self.stop_evt.is_set():
            t_cycle_start = time.perf_counter()
            now = dt.datetime.now()

            pv = sv = cur = {"value": None}
            try:
                with self.io_lock:
                    pv  = self.cwf.read_e5cd_pv_decimal(node=E5CD_NODE, sid=SID)
                    sv  = self.cwf.read_e5cd_sv_decimal(node=E5CD_NODE, sid=SID)
                    cur = self.cwf.read_g3pw_current_amps(node=G3PW_NODE, sid=SID)
            except Exception as e:
                print("[ERR] poll I/O:", e, file=sys.stderr)

            self.result_q.put((now, pv, sv, cur))

            # 周期調整
            spent = time.perf_counter() - t_cycle_start
            sleep = max(0.0, POLL_MS/1000.0 - spent)
            if self.stop_evt.wait(timeout=sleep):
                break

    # ---- メインスレッド：UI 反映 ----
    def drain_results(self):
        """キューに溜まった結果を UI に反映"""
        try:
            while True:
                now, pv, sv, cur = self.result_q.get_nowait()

                # 生データ保持（履歴は開始からずっと保持）
                if pv.get("value") is not None:
                    self.times_temp.append(now); self.vals_temp.append(pv["value"])
                if cur.get("value") is not None:
                    self.times_i.append(now);  self.vals_i.append(cur["value"])

                # SV表示
                if sv.get("value") is not None:
                    self.sv_value = sv["value"]
                    try:
                        sv_value_float = float(self.sv_value)
                        sv_text = f"{sv_value_float:.1f} °C"
                    except (TypeError, ValueError):
                        sv_text = str(self.sv_value)
                    self.lbl_sv.config(text=sv_text)

                # 直近1分エネルギー[Wh]を計算→履歴として蓄積（表示は開始→現在）
                if self.times_i:
                    energy_wh = self._integrate_energy_wh(
                        self.times_i, self.vals_i, now=now, voltage_v=VOLTAGE_V, window_sec=60.0
                    )
                    if energy_wh is not None:
                        self.times_energy.append(now)
                        self.vals_energy.append(energy_wh)

                # === グラフ更新 ===
                t_start = self.t0
                t_end   = now

                # 温度（開始→現在）
                if self.times_temp:
                    self.line_temp.set_data(self.times_temp, self.vals_temp)
                    self.ax_temp.set_xlim(t_start, t_end)
                    ymin, ymax = min(self.vals_temp), max(self.vals_temp)
                    if ymin == ymax:
                        ymin -= 1; ymax += 1
                    self.ax_temp.set_ylim(ymin, ymax)

                # Energy(rolling 1min の値を、開始→現在のX軸に沿って表示)
                if self.times_energy:
                    self.line_energy.set_data(self.times_energy, self.vals_energy)
                    self.ax_energy.set_xlim(t_start, t_end)
                    ymin, ymax = min(self.vals_energy), max(self.vals_energy)
                    if ymin == ymax:
                        pad = max(1e-6, ymax * 0.05)
                        ymin -= pad; ymax += pad
                    self.ax_energy.set_ylim(ymin, ymax)

                self.canvas.draw_idle()
        except queue.Empty:
            pass

        # 次の UI 更新
        if not self.stop_evt.is_set():
            self.after(100, self.drain_results)

    def on_close(self):
        try:
            self.stop_evt.set()
            if hasattr(self, "worker") and self.worker.is_alive():
                self.worker.join(timeout=1.0)
            if hasattr(self, "cwf") and self.cwf:
                self.cwf.close()
        finally:
            self.destroy()

if __name__ == "__main__":
    App().mainloop()
