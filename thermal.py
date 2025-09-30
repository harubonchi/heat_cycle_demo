#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E5CD に CompoWay/F コマンドを高頻度ポーリングして結果を print する最小スクリプト
- フレーム: STX | Node(2) Sub(2) SID(2) | CMD(ASCII HEX) | ETX | BCC
- ここでは Node=01, Sub=00, SID=00 （必要に応じて変更）
- C0 系: 1要素=8桁HEX（ASCII）想定（小数点位置は別読みしない＝生値表示）
- できるだけ速く回すため、短いtimeout＋明示デッドライン＋バッファ掃除
"""

import time
import serial

STX, ETX = 0x02, 0x03

# ====== 設定 ======
PORT = "/dev/ttyUSB0"   # Windowsなら "COM5" など
BAUD = 9600             # 速くしたいなら 19200/38400 に（機器設定と一致させる）
NODE = "01"
SUB  = "00"
SID  = "0"

# ★ 送るコマンド（ASCII HEX）
#   例: C0 領域 読み出し（要素数=1）→ "0101C00000000001"
#   例: 8000 領域 読み出し（要素数=1）→ "0101800000000001"
CMD  = "0101C00000000001"   # 必要に応じて切替

# ポーリング周期（秒）。できるだけ短く回すなら 0.05〜0.2 あたりから
POLL_SEC = 0.10

# 受信デッドライン（1回の往復で待つ最大時間）。短くしすぎると取りこぼす
RX_DEADLINE_SEC = 0.20


def bcc_ascii_hex(payload: bytes) -> bytes:
    """BCC = Node..ETX を XOR"""
    x = 0
    for b in payload:
        x ^= b
    return bytes([x & 0xFF])


def make_frame(node_hex: str, sub_hex: str, sid_hex: str, cmd_hex: str) -> bytes:
    """送信フレーム組み立て（STX/ETX/BCC含む）"""
    body = (node_hex + sub_hex + sid_hex + cmd_hex).encode("ascii")
    payload = body + bytes([ETX])
    return bytes([STX]) + payload + bcc_ascii_hex(payload)


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def parse_response(resp: bytes):
    """
    応答: STX | node(2) | sub(2) | end(2) | mres(2) | sres(2) | data... | ETX | BCC
    data は ASCII16進文字列
    """
    if len(resp) < 1 + 2 + 2 + 2 + 2 + 2 + 1 + 1:
        return {"ok": False, "err": "too short"}
    if resp[0] != STX or resp[-2] != ETX:
        return {"ok": False, "err": "bad STX/ETX"}
    # BCC確認
    if bcc_ascii_hex(resp[1:-1]) != resp[-1:]:
        return {"ok": False, "err": "BCC mismatch"}

    node = resp[1:3].decode()
    sub  = resp[3:5].decode()
    end  = resp[5:7].decode()
    mres = resp[7:9].decode()
    sres = resp[9:11].decode()
    data_hex = resp[11:-2].decode(errors="replace")
    return {"ok": True, "node": node, "sub": sub, "end": end,
            "mres": mres, "sres": sres, "data_hex": data_hex}


def recv_one_frame(ser: serial.Serial, deadline_sec: float) -> bytes | None:
    """STX待ち→ETXまで→BCC 1バイトの最小受信。deadline_sec を超えたら None"""
    end_time = time.perf_counter() + deadline_sec
    buf = bytearray()

    # 1) STX 待ち
    while time.perf_counter() < end_time:
        b = ser.read(1)
        if not b:
            continue
        if b[0] == STX:
            buf.extend(b)
            break
    else:
        return None  # timeout (STX来ず)

    # 2) 本体読み（ETXが出るまで断続的に読む）
    while time.perf_counter() < end_time:
        chunk = ser.read(256)
        if chunk:
            buf.extend(chunk)
            if ETX in chunk:
                # ETX の後ろに BCC が1バイト来るので追加で1バイト読む
                bcc = ser.read(1)
                if bcc:
                    buf.extend(bcc)
                break

    return bytes(buf) if len(buf) >= 3 else None


def main():
    frame = make_frame(NODE, SUB, SID, CMD)

    ser = serial.Serial(
        PORT,
        baudrate=BAUD,
        bytesize=serial.SEVENBITS, parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_TWO,
        timeout=0.01,              # 超短い分割タイムアウト（全体は deadline で管理）
        write_timeout=0.05
    )

    print(f"[INFO] PORT={PORT} BAUD={BAUD} POLL={POLL_SEC*1000:.0f}ms CMD={CMD}")
    print(f"[INFO] TX frame: {hexdump(frame)}")

    try:
        next_t = time.perf_counter()
        while True:
            t0 = time.perf_counter()

            # 前回の残りを掃除（ゴミで誤認しないように）
            ser.reset_input_buffer()

            # 送信
            ser.write(frame)
            ser.flush()

            # 受信
            resp = recv_one_frame(ser, RX_DEADLINE_SEC)
            t1 = time.perf_counter()

            if resp is None:
                print(f"{time.strftime('%H:%M:%S')} [TIMEOUT] no frame within {RX_DEADLINE_SEC*1000:.0f}ms")
            else:
                p = parse_response(resp)
                if not p["ok"]:
                    print(f"{time.strftime('%H:%M:%S')} [PARSE ERR] {p.get('err')}  RX={hexdump(resp)}")
                else:
                    # 正常/異常の表示とデータ部解釈（C0系＝8桁/要素想定）
                    if p["end"] != "00":
                        print(f"{time.strftime('%H:%M:%S')} [END={p['end']}] mres/sres={p['mres']}/{p['sres']} data='{p['data_hex']}'")
                    else:
                        d = p["data_hex"]
                        val_txt = ""
                        if len(d) >= 8:
                            raw_u32 = int(d[:8], 16)
                            # 符号付きに読み替え（負温度に備える）。小数点補正はしない（生値）
                            val_s32 = raw_u32 - 0x100000000 if (raw_u32 & 0x80000000) else raw_u32
                            val_txt = f" raw32=0x{raw_u32:08X} ({val_s32})"
                        elif len(d) >= 4:
                            raw_u16 = int(d[:4], 16)
                            val_s16 = raw_u16 - 0x10000 if (raw_u16 & 0x8000) else raw_u16
                            val_txt = f" raw16=0x{raw_u16:04X} ({val_s16})"
                        print(f"{time.strftime('%H:%M:%S')} [OK] mres/sres={p['mres']}/{p['sres']} data='{p['data_hex']}'{val_txt}")

            # 周期制御（できるだけ一定周期、ただし送受時間を考慮）
            next_t += POLL_SEC
            sleep = next_t - time.perf_counter()
            if sleep < 0:
                # 追いつけないときは今を基準にリセット
                next_t = time.perf_counter()
            else:
                time.sleep(sleep)

    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
