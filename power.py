#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G3PWへ CompoWay/F コマンド "0101CE0004000001" を1回だけ送信し、
1回分の応答を受け取り、人間が読める形に解釈してprintする最小スクリプト。
- フレーム形式: STX | Node(2) Sub(2) SID(1) | CMD(ASCII HEX) | ETX | BCC
- ここでは Node=02, Sub=00, SID=00 固定。
- 送信1回＋受信1回のみ。G3PWの電流(0.1A単位)をAに換算して出力。
"""

import serial
import time

STX, ETX = 0x02, 0x03
PORT = "/dev/ttyUSB0"   # 必要なら環境に合わせて変更
BAUD = 9600

NODE = "02"  # ノード番号（2桁BCD）
SUB  = "00"
SID  = "0"  # 1桁固定
CMD  = "01018E0004000001"  # 変数読出 MRC/SRC=01/01, CE:0004(Current) を1要素（0.1A単位）

def bcc_ascii_hex(payload: bytes) -> bytes:
    """BCC = Node〜ETX（STX除く）のXOR 1バイト"""
    x = 0
    for b in payload:
        x ^= b
    return bytes([x & 0xFF])

def make_frame(node_hex: str, sub_hex: str, sid_hex: str, cmd_hex: str) -> bytes:
    """
    CompoWay/F送信フレームを組み立てる（STX/ETX/BCC含む）。
    node_hex, sub_hex, sid_hex, cmd_hex はASCIIの16進文字列（2/2/2桁と可変）
    """
    body = (node_hex + sub_hex + sid_hex + cmd_hex).encode("ascii")
    payload = body + bytes([ETX])          # BCCは Node..ETX をXOR
    return bytes([STX]) + payload + bcc_ascii_hex(payload)

def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)

def parse_response(resp: bytes):
    """
    応答: STX | node(2) | sub(2) | end(2) | mres(2) | sres(2) | data... | ETX | BCC
    - end=="00" が正常。CE(ダブルワード)のdataは先頭8桁HEXが基本。
    """
    if len(resp) < 1 + 2 + 2 + 2 + 2 + 2 + 1 + 1:
        return {"ok": False, "err": "too short"}
    if resp[0] != STX or resp[-2] != ETX:
        return {"ok": False, "err": "bad STX/ETX"}

    # BCC確認（Node..ETX）
    if bcc_ascii_hex(resp[1:-1]) != resp[-1:]:
        return {"ok": False, "err": "BCC mismatch"}

    node = resp[1:3].decode()
    sub  = resp[3:5].decode()
    end  = resp[5:7].decode()
    mres = resp[7:9].decode()
    sres = resp[9:11].decode()
    data_hex = resp[11:-2].decode()  # 残り（ASCII16進）

    return {"ok": True, "node": node, "sub": sub, "end": end,
            "mres": mres, "sres": sres, "data_hex": data_hex}

def u32_to_s32(x: int) -> int:
    """32bit値を符号付きとして解釈（CE: ダブルワード想定。通常は正）"""
    return x - 0x100000000 if x & 0x80000000 else x

def main():
    frame = make_frame(NODE, SUB, SID, CMD)

    # 9600 / 7E2 でオープン
    ser = serial.Serial(
        PORT, baudrate=BAUD,
        bytesize=serial.SEVENBITS, parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_TWO,
        timeout=1.0
    )

    try:
        # 送信
        n = ser.write(frame)
        ser.flush()
        print(f"[TX] {hexdump(frame)}")
        print(f"Sent {n} bytes to {PORT}")

        # ---- 応答を1フレームだけ受信 ----
        buf = bytearray()
        deadline = time.time() + 1.2  # 受信タイムアウト

        # 1) STXを待つ
        while time.time() < deadline:
            b = ser.read(1)
            if not b:
                continue
            if b[0] == STX:
                buf.extend(b)
                break

        # 2) ETXまで読み、さらに1バイト（BCC）追い読み
        while buf and time.time() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
                if ETX in chunk:
                    buf.extend(ser.read(1))  # BCC
                    break

        if not buf:
            print("[RX] (timeout / no data)")
            return

        print(f"[RX] {hexdump(bytes(buf))}")

        # ---- 人間向けの解釈をprint ----
        p = parse_response(bytes(buf))
        if not p["ok"]:
            print("[PARSE]", p.get("err"))
            return

        print(f"[PARSE] end={p['end']} mres/sres={p['mres']}/{p['sres']} data='{p['data_hex']}'")

        if p["end"] != "00":
            # 代表例：0F=コマンドエラー（未対応等）
            print("[INFO] 機器がコマンドを受け付けませんでした（end!=00）。")
            return

        # CE: ダブルワード 1要素＝先頭8桁HEX（ASCII）を基本とする
        if len(p["data_hex"]) < 8:
            print("[INFO] データ長が足りません。")
            return

        raw_u32 = int(p["data_hex"][:8], 16)
        val_s32 = u32_to_s32(raw_u32)

        # ---- 追加：0.1A単位 → A に換算して表示 ----
        amps = val_s32 / 10.0
        print(f"[VALUE (A, from u32)] {amps}")

        # 念のため、末尾4桁（下位ワード）も十進/Aで併記（機器実装によってはこちらが実値のことも）
        if len(p["data_hex"]) >= 4:
            tail_dec = int(p["data_hex"][-4:], 16)
            print(f"[VALUE lower16 (decimal)] {tail_dec}")
            print(f"[VALUE lower16 (A)] {tail_dec / 10.0}")

    finally:
        ser.close()

if __name__ == "__main__":
    main()
