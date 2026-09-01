"""Build PS2-ready SFD replacements from subtitled viewing MP4 files.

The game's SFD files are fixed-size MPEG program streams containing MPEG-2
video and CRI ADX audio.  Every rebuilt movie is repacketized at access-unit boundaries and scheduled
against the original PS2 picture-arrival slots.  This keeps decoder/VBV pressure
close to the source stream even when re-encoding changes individual frame sizes.
Original ADX audio, pack headers, SCR values, and sector layout remain byte-identical.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from fractions import Fraction
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOV = ROOT / "assets" / "extraction" / "ps2" / "MOV"
SOURCE = MOV / "windows_viewable_sub"
RAW = MOV / "raw"
OUTPUT = MOV / "ps2_subtitled"
FFMPEG = Path(os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = Path(os.environ.get("FFPROBE") or shutil.which("ffprobe") or FFMPEG.with_name("ffprobe.exe"))
SUBTITLED = {"ERIKA", "KAIN", "KARERA", "LUNA", "LUNE", "OP", "SESIRU", "SUZU", "WENDY"}
PASSTHROUGH = {"HINAGIKU", "JYASHIN"}
WINDOWS_MOVIES = {"EPI", "PRO"}
REPACKETIZED = WINDOWS_MOVIES
SLOT_ALIGNED = SUBTITLED
REBUILT = WINDOWS_MOVIES | SUBTITLED


def subtitle_source(stem: str) -> Path:
    if stem == "OP":
        return MOV / "windows_viewable" / "OP.srt"
    return SOURCE / f"{stem}.srt"


def pes_payload_ranges(data: bytes, stream_id: int = 0xE0) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    i, n = 0, len(data)
    while i < n - 6:
        if data[i:i + 4] == b"\x00\x00\x01\xba":
            i += 12
            continue
        if data[i:i + 3] != b"\x00\x00\x01":
            i += 1
            continue
        sid = data[i + 3]
        if sid == 0xB9:
            i += 4
            continue
        length = int.from_bytes(data[i + 4:i + 6], "big")
        end = i + 6 + length
        if end > n:
            raise ValueError(f"truncated PES packet at {i:#x}")
        if sid == stream_id:
            p = i + 6
            while p < end and data[p] == 0xFF:
                p += 1
            if p + 1 < end and data[p] & 0xC0 == 0x40:
                p += 2
            if p < end:
                marker = data[p] & 0xF0
                if marker == 0x20:
                    p += 5
                elif marker == 0x30:
                    p += 10
                elif data[p] == 0x0F:
                    p += 1
                else:
                    raise ValueError(f"unknown PES optional header at {p:#x}")
            ranges.append((p, end))
        i = end
    if not ranges:
        raise ValueError("no video PES packets found")
    return ranges


def probe(path: Path) -> dict:
    cmd = [str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-count_frames",
           "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate",
           "-show_entries", "stream=nb_read_frames",
           "-show_entries", "format=duration", "-of", "json", str(path)]
    return json.loads(subprocess.check_output(cmd, text=True, encoding="utf-8"))


def mpeg_sequence_limits(data: bytes) -> tuple[int, int]:
    """Return sequence-header bit rate and VBV size in bits."""
    marker = data.find(b"\x00\x00\x01\xb3")
    if marker < 0 or marker + 12 > len(data):
        raise ValueError("MPEG sequence header not found")
    bits = "".join(f"{byte:08b}" for byte in data[marker + 4:marker + 12])
    bit_rate = int(bits[32:50], 2) * 400
    vbv_size = int(bits[51:61], 2) * 16_384
    return bit_rate, vbv_size


def ffmpeg_filter(stem: str, width: int, height: int, fps: str) -> str:
    filters = []
    if stem in SUBTITLED:
        srt = subtitle_source(stem).as_posix().replace(":", r"\:").replace("'", r"\'")
        style = "FontName=Malgun Gothic,FontSize=24,Outline=2,Shadow=0,MarginV=28,Alignment=2"
        filters.append(f"subtitles='{srt}':force_style='{style}'")
    filters.extend([f"scale={width}:{height}:flags=lanczos", "setsar=1"])
    if stem in REBUILT:
        # Retimestamp every source picture at the PS2 picture cadence.  This is
        # also important for EPI, whose MPEG-1 r_frame_rate reports field cadence
        # while avg_frame_rate reflects its actual picture cadence.
        filters.append(f"setpts=N/({fps}*TB)")
    return ",".join(filters)


def encode_video(stem: str, source: Path, original: Path, work: Path) -> tuple[Path, dict]:
    original_bytes = original.read_bytes()
    capacity = sum(b - a for a, b in pes_payload_ranges(original_bytes))
    meta = probe(original)
    stream = meta["streams"][0]
    duration = float(meta["format"]["duration"])
    width, height = stream["width"], stream["height"]
    fps = stream.get("avg_frame_rate") or stream["r_frame_rate"]
    if fps == "0/0":
        fps = stream["r_frame_rate"]
    target_frames = int(stream["nb_read_frames"])
    codec = "mpeg2video" if stream["codec_name"] == "mpeg2video" else "mpeg1video"
    out = work / f"{stem}.m2v"
    # Leave headroom for encoder variability and terminate the padded stream cleanly.
    start_rate = int(capacity * 8 / duration * 0.90)
    buffer_size = max(start_rate * 2, 1835008)
    b_frames = 2
    rate_factors = (1.0, 0.90, 0.80, 0.70, 0.60)
    if stem in REBUILT:
        # Match each PS2 movie's original sequence-header rate and decoder VBV.
        original_rate, buffer_size = mpeg_sequence_limits(original_bytes)
        start_rate = int(original_rate * 0.90)
        b_frames = 4 if stem == "PRO" else 2
        # A globally fitting encode can still overflow a local source-time
        # window. Retry lower rates until the movie's packetization strategy can
        # keep picture arrival close to the original stream.
        rate_factors = (1.0, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50,
                        0.45, 0.40, 0.35, 0.30)
    attempts = []
    for factor in rate_factors:
        rate = int(start_rate * factor)
        min_rate = 0 if stem in SLOT_ALIGNED else rate
        max_rate = original_rate if stem in SLOT_ALIGNED else rate
        cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-an", "-vf", ffmpeg_filter(stem, width, height, fps), "-r", fps,
               "-fps_mode", "cfr", "-frames:v", str(target_frames),
               "-c:v", codec, "-pix_fmt", "yuv420p", "-b:v", str(rate),
               "-minrate", str(min_rate), "-maxrate", str(max_rate),
               "-bufsize", str(buffer_size),
               "-g", "15", "-bf", str(b_frames), "-f", codec, str(out)]
        subprocess.run(cmd, check=True)
        size = out.stat().st_size
        encoded_frames = int(probe(out)["streams"][0]["nb_read_frames"])
        attempt = {"bitrate": rate, "bytes": size, "frames": encoded_frames}
        if encoded_frames != target_frames:
            raise RuntimeError(
                f"{stem}: encoded frame count mismatch: "
                f"expected={target_frames} actual={encoded_frames}"
            )
        if size + 4 <= capacity and stem in REBUILT:
            try:
                if stem in REPACKETIZED:
                    pressure = _source_arrival_pressure(original_bytes, out)
                else:
                    pressure = _source_aligned_payload_pressure(original_bytes, out)
            except ValueError as exc:
                attempt["source_arrival_error"] = str(exc)
                attempts.append(attempt)
                continue
            attempt.update(pressure)
            attempts.append(attempt)
            if stem == "PRO" and pressure["source_arrival_early_max_slots"] > 32:
                continue
            if stem in SLOT_ALIGNED and (
                pressure["source_arrival_early_max_slots"] > 128
                or pressure["source_arrival_late_max_slots"] > 256
            ):
                continue
            return out, {"capacity": capacity, "attempts": attempts,
                         "target": f"{width}x{height}@{fps}", "target_fps": fps,
                         "target_frames": target_frames, "codec": codec}
        attempts.append(attempt)
        if size + 4 <= capacity:
            return out, {"capacity": capacity, "attempts": attempts,
                         "target": f"{width}x{height}@{fps}", "target_fps": fps,
                         "target_frames": target_frames, "codec": codec}
    raise RuntimeError(f"{stem}: encoded video exceeds original capacity: {attempts}, capacity={capacity}")


def replace_video(original: Path, video_es: Path, output: Path) -> dict:
    data = bytearray(original.read_bytes())
    ranges = pes_payload_ranges(data)
    capacity = sum(b - a for a, b in ranges)
    video = video_es.read_bytes() + b"\x00\x00\x01\xb7"
    if len(video) > capacity:
        raise ValueError(f"replacement is {len(video)} bytes, capacity is {capacity}")
    video += b"\x00" * (capacity - len(video))
    cursor = 0
    for start, end in ranges:
        count = end - start
        data[start:end] = video[cursor:cursor + count]
        cursor += count
    output.write_bytes(data)
    return {"replacement_video_bytes": video_es.stat().st_size, "video_capacity": capacity,
            "output_bytes": len(data)}


def _encode_timestamp(value: int, prefix: int) -> bytes:
    value &= (1 << 33) - 1
    return bytes((
        (prefix << 4) | (((value >> 30) & 7) << 1) | 1,
        (value >> 22) & 0xFF,
        (((value >> 15) & 0x7F) << 1) | 1,
        (value >> 7) & 0xFF,
        ((value & 0x7F) << 1) | 1,
    ))


def _video_pes_slots(data: bytes) -> list[dict]:
    slots = []
    i, n = 0, len(data)
    while i < n - 6:
        if data[i:i + 4] == b"\x00\x00\x01\xba":
            i += 12
            continue
        if data[i:i + 3] != b"\x00\x00\x01":
            i += 1
            continue
        sid = data[i + 3]
        if sid == 0xB9:
            i += 4
            continue
        length = int.from_bytes(data[i + 4:i + 6], "big")
        end = i + 6 + length
        if end > n:
            raise ValueError(f"truncated PES packet at {i:#x}")
        if sid == 0xE0:
            header_start = i + 6
            p = header_start
            while p < end and data[p] == 0xFF:
                p += 1
            std = b""
            if p + 1 < end and data[p] & 0xC0 == 0x40:
                std = bytes(data[p:p + 2])
                p += 2
            marker = data[p] & 0xF0
            if marker == 0x20:
                p += 5
            elif marker == 0x30:
                p += 10
            elif data[p] == 0x0F:
                p += 1
            else:
                raise ValueError(f"unknown video PES header at {p:#x}")
            slots.append({"header_start": header_start, "payload_start": p,
                          "end": end, "std": std})
        i = end
    return slots


def _patch_sequence_limits(es: bytearray, bit_rate: int, vbv_bits: int) -> None:
    bit_rate_value = bit_rate // 400
    vbv_value = vbv_bits // 16_384
    marker = b"\x00\x00\x01\xb3"
    cursor = 0
    found = 0
    while True:
        pos = es.find(marker, cursor)
        if pos < 0:
            break
        p = pos + 4
        if p + 8 > len(es):
            raise ValueError("truncated MPEG sequence header")
        es[p + 4] = (bit_rate_value >> 10) & 0xFF
        es[p + 5] = (bit_rate_value >> 2) & 0xFF
        es[p + 6] = ((bit_rate_value & 3) << 6) | 0x20 | ((vbv_value >> 5) & 0x1F)
        es[p + 7] = (es[p + 7] & 0x07) | ((vbv_value & 0x1F) << 3)
        found += 1
        cursor = p + 8
    if not found:
        raise ValueError("no MPEG sequence headers to patch")


def _access_units(video_es: Path) -> list[dict]:
    cmd = [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
           "-show_frames", "-show_entries", "frame=pkt_pos,pict_type",
           "-of", "json", str(video_es)]
    frames = json.loads(subprocess.check_output(cmd, text=True, encoding="utf-8"))["frames"]
    units = []
    for display_index, frame in enumerate(frames):
        if "pkt_pos" not in frame:
            raise ValueError(f"frame {display_index} has no elementary-stream position")
        units.append({"start": int(frame["pkt_pos"]), "type": frame["pict_type"],
                      "display_index": display_index})
    units.sort(key=lambda unit: unit["start"])
    if not units or units[0]["start"] != 0:
        raise ValueError("first MPEG access unit does not start at byte zero")
    if len({unit["start"] for unit in units}) != len(units):
        raise ValueError("duplicate MPEG access-unit positions")
    for decode_index, unit in enumerate(units):
        unit["decode_index"] = decode_index
    return units


def _original_picture_slots(data: bytes, slots: list[dict]) -> list[int]:
    """Return the source video-PES slot containing each MPEG picture start.

    Picture start codes are in elementary-stream decode order.  Mapping those
    starts back to the original PS2 packet layout gives us a decoder-safe
    arrival profile without depending on sparse source PES timestamps.
    """
    payload = bytearray()
    cumulative_ends: list[int] = []
    for slot in slots:
        payload.extend(data[slot["payload_start"]:slot["end"]])
        cumulative_ends.append(len(payload))

    marker = b"\x00\x00\x01\x00"
    picture_offsets: list[int] = []
    cursor = 0
    while True:
        pos = payload.find(marker, cursor)
        if pos < 0:
            break
        picture_offsets.append(pos)
        cursor = pos + len(marker)
    return [bisect_right(cumulative_ends, pos) for pos in picture_offsets]


def _latest_unit_starts(slots: list[dict], unit_sizes: list[int]) -> list[int]:
    """Pack units backwards to find each unit's latest safe start slot."""
    latest = [0] * len(unit_sizes)
    cursor = len(slots)
    for unit_index in range(len(unit_sizes) - 1, -1, -1):
        remaining = unit_sizes[unit_index]
        start = cursor
        while remaining > 0:
            start -= 1
            if start < 0:
                raise ValueError("repacketized video cannot fit in original PES slots")
            remaining -= slots[start]["end"] - slots[start]["payload_start"]
        latest[unit_index] = start
        cursor = start
    return latest


def _source_arrival_pressure(original_data: bytes, video_es: Path) -> dict:
    """Measure how far future pictures must be pulled ahead to make the ES fit."""
    slots = _video_pes_slots(original_data)
    target_slots = _original_picture_slots(original_data, slots)
    units = _access_units(video_es)
    if len(target_slots) != len(units):
        raise ValueError(
            f"source/new picture count mismatch: source={len(target_slots)} new={len(units)}"
        )
    starts = [unit["start"] for unit in units] + [video_es.stat().st_size]
    unit_sizes = [starts[i + 1] - starts[i] for i in range(len(units))]
    latest_starts = _latest_unit_starts(slots, unit_sizes)
    early = [max(0, target_slots[i] - latest_starts[i]) for i in range(len(units))]
    max_early = max(early, default=0)
    max_index = early.index(max_early) if early else 0
    return {
        "source_arrival_early_max_slots": max_early,
        "source_arrival_early_max_frame": max_index,
    }


def _source_aligned_payload_plan(original_data: bytes, video_es: Path) -> dict:
    """Plan an ES layout that keeps picture starts near the original payload positions.

    Unlike access-unit PES repacketization, this plan keeps every original PES
    header and timestamp byte untouched.  Legal MPEG user-data padding is
    inserted only between encoded access units, so short source movies that put
    multiple pictures in one PES do not pay one-new-PES-per-picture overhead.
    """
    slots = _video_pes_slots(original_data)
    capacity = sum(slot["end"] - slot["payload_start"] for slot in slots)
    payload = bytearray()
    cumulative_ends: list[int] = []
    for slot in slots:
        payload.extend(original_data[slot["payload_start"]:slot["end"]])
        cumulative_ends.append(len(payload))

    target_picture_offsets: list[int] = []
    marker = b"\x00\x00\x01\x00"
    cursor = 0
    while True:
        pos = payload.find(marker, cursor)
        if pos < 0:
            break
        target_picture_offsets.append(pos)
        cursor = pos + len(marker)

    es = video_es.read_bytes()
    units = _access_units(video_es)
    if len(target_picture_offsets) != len(units):
        raise ValueError(
            f"source/new picture count mismatch: source={len(target_picture_offsets)} new={len(units)}"
        )
    unit_input_starts = [unit["start"] for unit in units] + [len(es)]
    unit_sizes = [unit_input_starts[i + 1] - unit_input_starts[i] for i in range(len(units))]
    relative_picture_offsets: list[int] = []
    for i in range(len(units)):
        picture = es.find(marker, unit_input_starts[i], unit_input_starts[i + 1])
        if picture < 0:
            raise ValueError(f"access unit {i} has no MPEG picture start")
        relative_picture_offsets.append(picture - unit_input_starts[i])

    desired_starts = [
        target_picture_offsets[i] - relative_picture_offsets[i]
        for i in range(len(units))
    ]
    latest_starts = [0] * len(units)
    cursor = capacity
    for i in range(len(units) - 1, -1, -1):
        cursor -= unit_sizes[i]
        if cursor < 0:
            raise ValueError("replacement ES exceeds original video payload capacity")
        latest_starts[i] = cursor

    output_starts: list[int] = []
    early_slots: list[int] = []
    late_slots: list[int] = []
    cursor = 0
    for i in range(len(units)):
        if i == 0:
            start = 0
        else:
            start = min(max(cursor, desired_starts[i]), latest_starts[i])
        if start < cursor:
            raise ValueError(
                f"source-aligned payload became infeasible at frame {i}: "
                f"current={cursor} desired={desired_starts[i]} latest={latest_starts[i]}"
            )
        actual_picture = start + relative_picture_offsets[i]
        target_slot = bisect_right(cumulative_ends, target_picture_offsets[i])
        actual_slot = bisect_right(cumulative_ends, actual_picture)
        early_slots.append(max(0, target_slot - actual_slot))
        late_slots.append(max(0, actual_slot - target_slot))
        output_starts.append(start)
        cursor = start + unit_sizes[i]

    max_early = max(early_slots, default=0)
    max_late = max(late_slots, default=0)
    return {
        "slots": slots,
        "capacity": capacity,
        "unit_input_starts": unit_input_starts,
        "unit_output_starts": output_starts,
        "source_arrival_early_max_slots": max_early,
        "source_arrival_early_max_frame": early_slots.index(max_early) if early_slots else 0,
        "source_arrival_late_max_slots": max_late,
        "source_arrival_late_max_frame": late_slots.index(max_late) if late_slots else 0,
    }


def _source_aligned_payload_pressure(original_data: bytes, video_es: Path) -> dict:
    plan = _source_aligned_payload_plan(original_data, video_es)
    return {
        "source_arrival_early_max_slots": plan["source_arrival_early_max_slots"],
        "source_arrival_early_max_frame": plan["source_arrival_early_max_frame"],
        "source_arrival_late_max_slots": plan["source_arrival_late_max_slots"],
        "source_arrival_late_max_frame": plan["source_arrival_late_max_frame"],
    }


def replace_video_slot_aligned(original: Path, video_es: Path, output: Path) -> dict:
    """Replace video while preserving original PES headers/PTS/DTS byte-for-byte."""
    original_data = original.read_bytes()
    plan = _source_aligned_payload_plan(original_data, video_es)
    data = bytearray(original_data)
    original_rate, original_vbv = mpeg_sequence_limits(data)
    es = bytearray(video_es.read_bytes())
    _patch_sequence_limits(es, original_rate, original_vbv)

    linear = bytearray()
    input_starts = plan["unit_input_starts"]
    output_starts = plan["unit_output_starts"]
    for i, output_start in enumerate(output_starts):
        if output_start < len(linear):
            raise ValueError(f"overlapping aligned access unit at frame {i}")
        linear.extend(_end_padding(output_start - len(linear)))
        linear.extend(es[input_starts[i]:input_starts[i + 1]])
    if len(linear) > plan["capacity"]:
        raise ValueError("aligned replacement exceeded original payload capacity")
    linear.extend(_end_padding(plan["capacity"] - len(linear)))
    if len(linear) != plan["capacity"]:
        raise ValueError("aligned replacement payload size mismatch")

    cursor = 0
    for slot in plan["slots"]:
        count = slot["end"] - slot["payload_start"]
        data[slot["payload_start"]:slot["end"]] = linear[cursor:cursor + count]
        cursor += count
    output.write_bytes(data)
    return {
        "replacement_video_bytes": len(es),
        "video_capacity": plan["capacity"],
        "output_bytes": len(data),
        "slot_aligned_payload": True,
        "payload_padding_bytes": plan["capacity"] - len(es),
        "source_arrival_early_max_slots": plan["source_arrival_early_max_slots"],
        "source_arrival_early_max_frame": plan["source_arrival_early_max_frame"],
        "source_arrival_late_max_slots": plan["source_arrival_late_max_slots"],
        "source_arrival_late_max_frame": plan["source_arrival_late_max_frame"],
        "sequence_bit_rate": original_rate,
        "sequence_vbv_bits": original_vbv,
    }


def _pes_header(size: int, std: bytes, pts: int | None, dts: int | None) -> bytes:
    if pts is None:
        stamp = b"\x0f"
    elif dts is None:
        stamp = _encode_timestamp(pts, 2)
    else:
        stamp = _encode_timestamp(pts, 3) + _encode_timestamp(dts, 1)
    stuffing = size - len(std) - len(stamp)
    if stuffing < 0:
        raise ValueError(f"PES optional header is too small: {size}")
    return b"\xff" * stuffing + std + stamp


def _end_padding(size: int) -> bytes:
    if size >= 4:
        return b"\x00\x00\x01\xb2" + b"\xff" * (size - 4)
    return b"\x00" * size


def replace_video_repacketized(
    original: Path, video_es: Path, output: Path, *, fps: str,
    preserve_source_arrival: bool = False,
) -> dict:
    data = bytearray(original.read_bytes())
    slots = _video_pes_slots(data)
    capacity = sum(slot["end"] - slot["payload_start"] for slot in slots)
    original_rate, original_vbv = mpeg_sequence_limits(data)
    es = bytearray(video_es.read_bytes())
    _patch_sequence_limits(es, original_rate, original_vbv)
    units = _access_units(video_es)
    starts = [unit["start"] for unit in units] + [len(es)]
    unit_sizes = [starts[i + 1] - starts[i] for i in range(len(units))]
    target_slots: list[int] | None = None
    latest_starts: list[int] | None = None
    if preserve_source_arrival:
        target_slots = _original_picture_slots(data, slots)
        if len(target_slots) != len(units):
            raise ValueError(
                f"source/new picture count mismatch: source={len(target_slots)} new={len(units)}"
            )
        latest_starts = _latest_unit_starts(slots, unit_sizes)

    slot_index = 0
    padding_slots = 0
    schedule_early_max = 0
    schedule_late_max = 0
    fps_fraction = Fraction(fps)

    def frame_timestamp(index: int) -> int:
        return round(Fraction(index * 90_000 * fps_fraction.denominator,
                              fps_fraction.numerator))

    for unit_index, unit in enumerate(units):
        if target_slots is not None and latest_starts is not None:
            target_slot = target_slots[unit_index]
            start_slot = min(max(slot_index, target_slot), latest_starts[unit_index])
            if start_slot < slot_index:
                raise ValueError(
                    f"source-arrival schedule became infeasible at frame {unit_index}: "
                    f"current={slot_index} target={target_slot} latest={latest_starts[unit_index]}"
                )
            schedule_early_max = max(schedule_early_max, target_slot - start_slot)
            schedule_late_max = max(schedule_late_max, start_slot - target_slot)
            for slot in slots[slot_index:start_slot]:
                header_size = slot["payload_start"] - slot["header_start"]
                data[slot["header_start"]:slot["payload_start"]] = _pes_header(
                    header_size, slot["std"], None, None)
                data[slot["payload_start"]:slot["end"]] = _end_padding(
                    slot["end"] - slot["payload_start"])
            padding_slots += start_slot - slot_index
            slot_index = start_slot

        chunk = memoryview(es)[starts[unit_index]:starts[unit_index + 1]]
        cursor = 0
        first_packet = True
        while cursor < len(chunk):
            if slot_index >= len(slots):
                raise ValueError(f"repacketized video exceeds original capacity at frame {unit_index}")
            slot = slots[slot_index]
            header_size = slot["payload_start"] - slot["header_start"]
            capacity_here = slot["end"] - slot["payload_start"]
            if first_packet:
                pts = frame_timestamp(unit["display_index"])
                dts = frame_timestamp(unit["decode_index"]) if unit["type"] in ("I", "P") else None
            else:
                pts = dts = None
            data[slot["header_start"]:slot["payload_start"]] = _pes_header(
                header_size, slot["std"], pts, dts)
            count = min(capacity_here, len(chunk) - cursor)
            data[slot["payload_start"]:slot["payload_start"] + count] = chunk[cursor:cursor + count]
            cursor += count
            if count < capacity_here:
                data[slot["payload_start"] + count:slot["end"]] = _end_padding(capacity_here - count)
            slot_index += 1
            first_packet = False

    trailing_padding_slots = len(slots) - slot_index
    for slot in slots[slot_index:]:
        header_size = slot["payload_start"] - slot["header_start"]
        data[slot["header_start"]:slot["payload_start"]] = _pes_header(
            header_size, slot["std"], None, None)
        padding_size = slot["end"] - slot["payload_start"]
        data[slot["payload_start"]:slot["end"]] = (
            _end_padding(padding_size) if preserve_source_arrival else b"\xff" * padding_size
        )

    output.write_bytes(data)
    return {"replacement_video_bytes": len(es), "video_capacity": capacity,
            "video_pes_packets_used": slot_index - padding_slots,
            "video_pes_packets_padding": padding_slots + trailing_padding_slots,
            "video_pes_packets_total": len(slots),
            "output_bytes": len(data), "repacketized": True,
            "source_arrival_schedule": preserve_source_arrival,
            "timestamp_fps": fps,
            "schedule_early_max_slots": schedule_early_max,
            "schedule_late_max_slots": schedule_late_max,
            "sequence_bit_rate": original_rate, "sequence_vbv_bits": original_vbv}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", help="process only this stem (repeatable)")
    args = parser.parse_args()
    requested = set(args.only or (SUBTITLED | PASSTHROUGH | REPACKETIZED))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    work = OUTPUT / "_encoded_video"
    work.mkdir(exist_ok=True)
    records = []
    for stem in sorted(requested):
        original = RAW / f"{stem}.SFD"
        output = OUTPUT / f"{stem}.SFD"
        if stem in PASSTHROUGH:
            shutil.copy2(original, output)
            records.append({"name": stem, "mode": "original_passthrough", "output": output.name,
                            "output_bytes": output.stat().st_size})
            print(f"{stem}: copied original")
            continue
        if stem == "OP":
            source = MOV / "windows_viewable" / "OP.mp4"
        elif stem == "EPI":
            source = OUTPUT / "windows_viewable" / "EPI.mpg"
        else:
            source = SOURCE / ("PRO.mpg" if stem == "PRO" else f"{stem}.mp4")
        video_es, enc = encode_video(stem, source, original, work)
        if stem in REPACKETIZED:
            mux = replace_video_repacketized(
                original, video_es, output, fps=enc["target_fps"], preserve_source_arrival=True)
        elif stem in SLOT_ALIGNED:
            mux = replace_video_slot_aligned(original, video_es, output)
        else:
            mux = replace_video(original, video_es, output)
        mode = "windows_movie" if stem in WINDOWS_MOVIES else "subtitle_burn_in"
        records.append({"name": stem, "mode": mode,
                        "source": source.name, "subtitle": f"{stem}.srt" if stem in SUBTITLED else None,
                        "output": output.name, **enc, **mux})
        print(f"{stem}: rebuilt {output.name}")
    manifest = {"format": "digicarr-fe-ps2-subtitled-sfd-v1",
                "note": "Original ADX audio, pack headers, SCR values, and sector layout preserved. EPI/PRO use access-unit PES repacketization; subtitle-only movies preserve source PES headers and use source-aligned ES padding.",
                "records": records}
    manifest_path = OUTPUT / "manifest.json"
    if args.only and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        replaced = {record["name"] for record in records}
        records = [record for record in previous.get("records", []) if record.get("name") not in replaced] + records
        records.sort(key=lambda record: record["name"])
        manifest["records"] = records
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
