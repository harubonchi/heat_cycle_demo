# compowayf_driver.py
# -*- coding: utf-8 -*-
"""
CompoWay/F minimal driver class (SID = 1 digit)
- Linux default: /dev/ttyUSB0 @ 9600, 7E2
- 公開API:
    - read_e5cd_pv_decimal(node="01", sid="0")
        -> {"cmd_hex": "<Node+Sub+SID+CMD>", "value": <int or None>}
        ※ 実機ログ/参照スクリプトに合わせて「末尾4桁（下位ワード）」を10進化
    - read_e5cd_sv_decimal(node="01", sid="0")
        -> {"cmd_hex": "<Node+Sub+SID+CMD>", "value": <int or None>}
        ※ 送信コマンドは元コードのまま(0101810003000001)。コメントにあった 0101C1... と不一致注意。
    - read_g3pw_current_amps(node="02", sid="0")
        -> {"cmd_hex": "<Node+Sub+SID+CMD>", "value": <float or None>}
        ※ CE:0004 は 0.1A単位 → A。先頭8桁/10 を基本、0の場合は末尾4桁/10 をフォールバック
"""

import time
import serial

STX, ETX = 0x02, 0x03

class CompoWayFDriver:
    def __init__(
        self,
        port="/dev/ttyUSB0",
        baudrate=9600,
        timeout=0,        # ★ 短い分割timeout（実際の受信猶予は rx_deadline で管理）
        write_timeout=0,  # ★ 送信停滞の早期検出
        rx_deadline=0.25    # ★ 1往復の最大待ち（ETX＋BCCまで）
    ):
        self.ser = serial.Serial(
            port,
            baudrate=baudrate,
            bytesize=serial.SEVENBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_TWO,
            timeout=timeout,
            write_timeout=write_timeout,
        )
        self.rx_deadline = float(rx_deadline)

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): self.close()
    def close(self):
        if getattr(self, "ser", None) and self.ser.is_open:
            self.ser.close()

    # ------------- low-level helpers -------------
    @staticmethod
    def _bcc_ascii_hex(payload: bytes) -> bytes:
        x = 0
        for b in payload: x ^= b
        return bytes([x & 0xFF])

    @staticmethod
    def _z2(s: str) -> str:
        return s.zfill(2)

    def _make_frame(self, node_hex: str, sub_hex: str, sid_ascii: str, cmd_hex: str) -> bytes:
        """
        Frame: [STX][Node(2)][Sub(2)][SID(1)][CMD(ASCII HEX...)][ETX][BCC]
        ※ SIDは1桁（"0"〜"9"）を想定
        """
        node_hex = self._z2(node_hex)
        sub_hex  = self._z2(sub_hex)
        if not sid_ascii or len(sid_ascii) != 1:
            raise ValueError("SID must be exactly ONE ASCII char (e.g., '0').")
        body = (node_hex + sub_hex + sid_ascii + cmd_hex).encode("ascii")
        payload = body + bytes([ETX])              # BCCは Node..ETX
        return bytes([STX]) + payload + self._bcc_ascii_hex(payload)

    def _read_one_response(self, deadline_s: float) -> bytes:
        """
        STX…ETX+BCC を1フレーム受信。ETXまで到達しなければ b'' を返す。
        deadline_s: 受信全体の猶予（serial.timeoutより長くできる）
        """
        end_time = time.perf_counter() + float(deadline_s)

        # 1) STX待ち
        buf = bytearray()
        while time.perf_counter() < end_time:
            b = self.ser.read(1)
            if not b:
                continue
            if b[0] == STX:
                buf.extend(b)
                break
        else:
            return b""  # STXが来なかった

        # 2) ETXまで（断続的に読み続け、見つかったらBCC 1バイト追い読み）
        seen_etx = False
        while time.perf_counter() < end_time:
            chunk = self.ser.read(256)
            if not chunk:
                continue
            buf.extend(chunk)
            if ETX in chunk:
                seen_etx = True
                bcc = self.ser.read(1)  # BCC
                if bcc:
                    buf.extend(bcc)
                break

        if not seen_etx:
            return b""  # 不完全フレームは破棄

        return bytes(buf)

    @staticmethod
    def _parse_response(resp: bytes):
        """
        応答: STX | node(2) | sub(2) | end(2) | mres(2) | sres(2) | data... | ETX | BCC
        """
        if not resp or len(resp) < 1 + 2 + 2 + 2 + 2 + 2 + 1 + 1:
            return {"ok": False, "err": "too short", "raw": resp}
        if resp[0] != STX or resp[-2] != ETX:
            return {"ok": False, "err": "bad STX/ETX", "raw": resp}
        # BCC check（Node..ETX）
        calc = CompoWayFDriver._bcc_ascii_hex(resp[1:-1])
        if calc != resp[-1:]:
            return {"ok": False, "err": "BCC mismatch", "raw": resp}

        try:
            node  = resp[1:3].decode()
            sub   = resp[3:5].decode()
            end   = resp[5:7].decode()
            mres  = resp[7:9].decode()
            sres  = resp[9:11].decode()
            data_hex = resp[11:-2].decode(errors="replace")
        except Exception as e:
            return {"ok": False, "err": f"decode error: {e}", "raw": resp}

        return {"ok": True, "node": node, "sub": sub, "end": end,
                "mres": mres, "sres": sres, "data_hex": data_hex, "raw": resp}

    def _send_and_get(self, node: str, cmd_hex: str, sid: str = "0", sub: str = "00"):
        """
        1コマンド送信→1応答受信→パース。
        戻りの cmd_hex_sent は「Node+Sub+SID+CMD」（ASCII連結）。
        """
        frame = self._make_frame(node_hex=node, sub_hex=sub, sid_ascii=sid, cmd_hex=cmd_hex)
        # 前回残りの除去（誤検知/遅延を避ける）
        self.ser.reset_input_buffer()
        # 送信
        self.ser.write(frame)
        self.ser.flush()
        # 受信（ETXまで）
        resp = self._read_one_response(deadline_s=self.rx_deadline)
        parsed = self._parse_response(resp) if resp else {"ok": False, "err": "timeout", "raw": b""}
        cmd_hex_sent = (self._z2(node) + self._z2(sub) + sid + cmd_hex)
        return cmd_hex_sent, parsed

    # ------------- public API -------------
    def read_e5cd_pv_decimal(self, node: str = "01", sid: str = "0"):
        """
        E5CD: PV (C0:0000) を1回読み、10進整数として返す。
        実機ログ/参照スクリプトに合わせ、**末尾4桁（下位ワード）**を10進化して返す。
        戻り: {"cmd_hex": <Node+Sub+SID+CMD>, "value": <int or None>}
        """
        cmd = "0101800000000001"
        cmd_hex_sent, r = self._send_and_get(node=node, cmd_hex=cmd, sid=sid, sub="00")
        if not r.get("ok") or r.get("end") != "00" or len(r.get("data_hex","")) < 4:
            return {"cmd_hex": cmd_hex_sent, "value": None}
        tail_dec = int(r["data_hex"][-4:], 16)
        return {"cmd_hex": cmd_hex_sent, "value": tail_dec}

    def read_e5cd_sv_decimal(self, node: str = "01", sid: str = "0"):
        """
        E5CD: 設定温度（SV）を1回読み、10進整数として返す。
        注意: 元コードの送信値は "0101810003000001"（コメントの 0101C1... と不一致）。
              仕様に合わせる場合は cmd を差し替えてください。
        先頭8桁が非0ならそれを採用、0の場合は末尾4桁を採用（小数点補正なし）。
        戻り: {"cmd_hex": <Node+Sub+SID+CMD>, "value": <int or None>}
        """
        cmd = "0101810003000001"  # ←必要なら "0101C10003000001" に変更
        cmd_hex_sent, r = self._send_and_get(node=node, cmd_hex=cmd, sid=sid, sub="00")
        if not r.get("ok") or r.get("end") != "00" or len(r.get("data_hex","")) < 4:
            return {"cmd_hex": cmd_hex_sent, "value": None}

        data = r["data_hex"]
        head_val = int(data[:8], 16) if len(data) >= 8 else 0
        tail_val = int(data[-4:], 16)
        value = head_val if head_val != 0 else tail_val
        return {"cmd_hex": cmd_hex_sent, "value": value}

    def read_g3pw_current_amps(self, node: str = "02", sid: str = "0"):
        """
        G3PW: 電流 (CE:0004) を1回読み、Aに換算して返す（0.1A単位→/10）。
        先頭8桁/10 を基本、**先頭8桁が0で末尾4桁が非0**なら 末尾4桁/10 を採用。
        戻り: {"cmd_hex": <Node+Sub+SID+CMD>, "value": <float or None>}
        """
        cmd = "01018E0004000001"
        cmd_hex_sent, r = self._send_and_get(node=node, cmd_hex=cmd, sid=sid, sub="00")
        if not r.get("ok") or r.get("end") != "00" or len(r.get("data_hex","")) < 4:
            return {"cmd_hex": cmd_hex_sent, "value": None}

        data = r["data_hex"]
        head_u32 = int(data[:8], 16) if len(data) >= 8 else 0
        tail_u16 = int(data[-4:], 16)
        if head_u32 != 0:
            value = head_u32 / 10.0
        elif tail_u16 != 0:
            value = tail_u16 / 10.0
        else:
            value = 0.0
        return {"cmd_hex": cmd_hex_sent, "value": value}


# 簡易テスト（インポート時は実行されない）
if __name__ == "__main__":
    with CompoWayFDriver() as cwf:
        print(cwf.read_e5cd_pv_decimal(node="01", sid="0"))
        print(cwf.read_e5cd_sv_decimal(node="01", sid="0"))
        print(cwf.read_g3pw_current_amps(node="02", sid="0"))
