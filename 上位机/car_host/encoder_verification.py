"""Independent offline CRC/CSV/boundary verification of encoder recordings."""

import binascii
from collections import Counter
import csv
import json
from pathlib import Path
import struct


def verify_encoder_recording(directory):
    directory = Path(directory)
    raw = Counter()
    buffer = bytearray()
    errors = []
    discarded = 0
    for line in (directory/"communication.log").read_text(encoding="utf-8-sig").splitlines():
        if "] RX |" not in line or " | HEX=" not in line:
            continue
        buffer.extend(bytes.fromhex(line.split(" | HEX=", 1)[1]))
        while len(buffer) >= 16:
            if buffer[:2] != b"\xa5\x5a":
                errors.append("帧头"); del buffer[0]; discarded += 1; continue
            length = int.from_bytes(buffer[4:6], "little")
            if buffer[2] != 1 or length > 64:
                errors.append("版本/长度"); del buffer[0]; discarded += 1; continue
            size = length + 16
            if len(buffer) < size:
                break
            frame = bytes(buffer[:size]); del buffer[:size]
            if binascii.crc_hqx(frame[2:-2], 0xffff) != int.from_bytes(frame[-2:], "little"):
                errors.append("CRC"); continue
            if frame[3] == 0x87:
                raw[frame.hex()] += 1
    recorded = Counter()
    csv_path = directory/"encoder_totals.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig"))) if csv_path.exists() else []
    for row in rows:
        frame = bytes.fromhex(row["raw_frame_hex"])
        recorded[frame.hex()] += 1
        try:
            if frame[3] != 0x87 or len(frame) != 56:
                raise ValueError("类型/长度")
            values = struct.unpack("<HHIIIqqII", frame[14:-2])
            expected = dict(zip(("epoch_tick", "device_time_ms", "sample_seq", "total_left",
                "total_right", "interval_ms", "delayed_samples"), values[2:]))
            expected.update(device_session=int.from_bytes(frame[6:10], "little"),
                            frame_seq=int.from_bytes(frame[10:14], "little"))
            if values[0] != 1 or values[1] not in (0, 1) or any(int(row[k]) != v for k,v in expected.items()):
                raise ValueError("字段")
            if (row["initialized"] == "True") != bool(values[1]):
                raise ValueError("初始化标记")
        except (ValueError, KeyError, struct.error, IndexError) as exc:
            errors.append(str(exc))
    # Boundaries are explicit cached snapshots; they need not be a new RX row.
    starts, calibrations = [], []
    for line in (directory/"receive_trace.jsonl").read_text(encoding="utf-8-sig").splitlines():
        item = json.loads(line)
        if item.get("kind") == "encoder_calibration_start":
            starts.append(item["snapshot"])
        if item.get("kind") != "encoder_calibration_end":
            continue
        a,b = item["baseline"], item["end"]
        for snapshot in (a,b):
            frame = bytes.fromhex(snapshot["raw_frame"])
            if (len(frame) != 56 or frame[3] != 0x87
                or binascii.crc_hqx(frame[2:-2],0xffff) != int.from_bytes(frame[-2:],"little")):
                errors.append("边界CRC/长度")
                continue
            decoded = struct.unpack("<HHIIIqqII", frame[14:-2])
            expected = dict(zip(("epoch_tick","device_time_ms","sample_seq","total_left",
                "total_right","interval_ms","delayed_samples"),decoded[2:]))
            expected.update(device_session=int.from_bytes(frame[6:10],"little"),
                            frame_seq=int.from_bytes(frame[10:14],"little"))
            if decoded[0] != 1 or decoded[1] != 1 or any(snapshot[k] != v for k,v in expected.items()):
                errors.append("边界字段")
        if a not in starts or any(a[k] != b[k] for k in ("connection_id","device_session","epoch_tick","delayed_samples")):
            errors.append("边界身份或诊断不一致")
        deltas = [((b[k]-a[k]+(1<<63)) % (1<<64))-(1<<63) for k in ("total_left","total_right")]
        for side,delta in zip(("left","right"),deltas):
            turns = item[side+"_turns"]
            if type(turns) is not int or not 1 <= turns <= 100 or item["delta_"+side] != delta or item["counts_per_"+side+"_turn"] != abs(delta)/turns:
                errors.append("计数差/圈数计算")
        calibrations.append(item)
    missing, extra = sum((raw-recorded).values()), sum((recorded-raw).values())
    if missing or extra:
        errors.append("原始RX与CSV覆盖不一致")
    if buffer:
        errors.append("原始RX尾残片")
    integrity = (directory/"communication.log").read_text(encoding="utf-8-sig").splitlines()[-1]
    saved = integrity.startswith("INTEGRITY ") and "dropped_records=0 " in integrity and "failure=None" in integrity
    return dict(verified=bool(rows) and not errors and saved, csv_rows=len(rows),
                raw_rows=sum(raw.values()), missing=missing, extra=extra, errors=errors,
                saved=saved, calibration_count=len(calibrations), calibrations=calibrations)
