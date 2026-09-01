from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys

LOCAL_DEPS = Path(__file__).resolve().parents[1] / ".deps" / "build"
if LOCAL_DEPS.is_dir():
    sys.path.insert(0, str(LOCAL_DEPS))

import pycdlib
from PIL import Image

from credits_codec import encode_credit_payloads, load_credit_plan
from font_codec import (
    KANJI_CELL_BYTES,
    KANJI_CELL_HEIGHT,
    KANJI_CELL_WIDTH,
    collect_non_hangul_sjis_codes,
    collect_used_sjis_codes,
    decode_korean_text,
    encode_korean_text,
    first_level_kanji_codes,
    normalize_text,
    patch_kanji_font,
)
from formats import AfsArchive, Ps2Pak, read_iso_file, sha256_file
from image_formats import (
    brighten_texture_container,
    color_grade_texture_container,
    encode_five_tile_screen_like,
    encode_pvr_like,
    parse_pvr_header,
)
from name_input_ko import allocate_name_input_code_map, patch_name_input
from scx import ScxFile
from scenario_aux import build_auxiliary_plan, build_developer_tx_plan
from verify_inputs import check, load_config


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "assets" / "translation" / "scenario.json"
SYSTEM_PATH = ROOT / "assets" / "translation" / "system_strings.json"
SYSTEM_EXTRA_PATH = ROOT / "assets" / "translation" / "system_strings_ps2_extra.json"
SYSTEM_SCAN_PATH = ROOT / "assets" / "extraction" / "ps2" / "SLPM_653.95.japanese_strings_all.json"
ETC_IMAGE_READY_DIR = ROOT / "assets" / "translation" / "etc_images_manual_ps2_ready"
BG_IMAGE_READY_DIR = ROOT / "assets" / "translation" / "bg_images" / "kor"
MOVIE_READY_DIR = ROOT / "assets" / "extraction" / "ps2" / "MOV" / "ps2_subtitled"

# Measured from the translated, ungraded v26 assets. BG has a much darker
# luminance distribution than EVENT/FACE/ETC, so a single +RGB offset is not a
# good fit. This preset lifts dark/mid tones with gamma while keeping highlights
# and already-white UI substantially closer to the source artwork.
NATURAL_GAME_GRADE = {
    "ETC.PAK": {"brightness": 2, "gamma": 0.98, "contrast": 1.02, "saturation": 1.03},
    "BG.PAK": {"brightness": 6, "gamma": 0.86, "contrast": 1.06, "saturation": 1.08},
    "EVENT.PAK": {"brightness": 3, "gamma": 0.92, "contrast": 1.04, "saturation": 1.06},
    "FACE.PAK": {"brightness": 2, "gamma": 0.94, "contrast": 1.03, "saturation": 1.05},
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_required_inputs() -> Path:
    cfg = load_config()
    iso_path = (ROOT / cfg["ps2_iso"]["path"]).resolve()
    check(iso_path, cfg["ps2_iso"])
    return iso_path


def patch_iso_members(source_iso: Path, output_iso: Path, replacements: dict[str, bytes]) -> dict[str, dict[str, int]]:
    iso = pycdlib.PyCdlib()
    iso.open(str(source_iso))
    records: dict[str, tuple[int, int]] = {}
    try:
        for iso_member, payload in replacements.items():
            rec = iso.get_record(iso_path=iso_member)
            extent = rec.extent_location()
            length = rec.data_length
            if len(payload) != length:
                raise ValueError(
                    f"ISO in-place member size mismatch for {iso_member}: expected={length} actual={len(payload)}"
                )
            records[iso_member] = (extent, length)
    finally:
        iso.close()

    output_iso.parent.mkdir(parents=True, exist_ok=True)
    temp = output_iso.with_suffix(output_iso.suffix + ".tmp")
    if temp.exists():
        temp.unlink()
    shutil.copyfile(source_iso, temp)
    with temp.open("r+b") as f:
        for iso_member, payload in replacements.items():
            extent, _ = records[iso_member]
            f.seek(extent * 0x800)
            f.write(payload)
        f.flush()
    temp.replace(output_iso)
    return {name: {"extent": extent, "size": length} for name, (extent, length) in records.items()}


def effective_translation(row: dict, by_key: dict[str, dict]) -> tuple[str | None, str]:
    target = row.get("target_ko") or ""
    if target:
        return target, "direct"
    if row.get("status") == "translation_alias":
        source_key = row.get("alias_of")
        source = by_key.get(source_key)
        if source is None:
            raise ValueError(f"translation alias points to missing record: {row['key']} -> {source_key}")
        source_target = source.get("target_ko") or ""
        if source_target:
            return source_target, "alias"
    return None, "unchanged"


def system_slot_capacity(raw: bytes, offset: int, source_length: int) -> int:
    i = offset + source_length
    zero_run = 0
    while i < len(raw) and raw[i] == 0:
        zero_run += 1
        i += 1
    if zero_run < 1:
        raise ValueError(f"system string at {offset:#x} is not NUL terminated")
    return source_length + zero_run - 1


def load_system_replacements(raw: bytes, require_complete: bool) -> tuple[list[dict], Counter]:
    doc = json.loads(SYSTEM_PATH.read_text(encoding="utf-8"))
    rows = list(doc["records"])
    if SYSTEM_EXTRA_PATH.is_file():
        scan = json.loads(SYSTEM_SCAN_PATH.read_text(encoding="utf-8"))
        scan_by_offset = {int(row["file_offset"]): row for row in scan["records"]}
        occupied_offsets = {int(row["file_offset"]) for row in rows}
        extra = json.loads(SYSTEM_EXTRA_PATH.read_text(encoding="utf-8"))
        for item in extra.get("records", []):
            off = int(item["file_offset"])
            if off in occupied_offsets:
                raise ValueError(f"duplicate system translation offset: {off:#x}")
            scan_off = int(item.get("scan_offset", off))
            source = scan_by_offset.get(scan_off)
            if source is None:
                if "source_ja" not in item:
                    raise ValueError(f"extra system source offset not found in full scan: {scan_off:#x}")
                prefix = 0
                source_text = str(item["source_ja"])
                source_bytes = source_text.encode("cp932")
                source_length = int(item.get("source_byte_length", len(source_bytes)))
                if len(source_bytes) != source_length:
                    raise ValueError(f"explicit extra system source length mismatch at {off:#x}")
                source_section = str(item.get("section", ".rodata"))
            else:
                prefix = int(item.get("strip_prefix_bytes", 0))
                source_bytes = source["text"].encode("cp932")
                if prefix < 0 or prefix >= len(source_bytes):
                    raise ValueError(f"invalid extra system source prefix at {off:#x}: {prefix}")
                source_text = source_bytes[prefix:].decode("cp932")
                source_length = int(source["source_byte_length"]) - prefix
                source_section = source["section"]
            rows.append({
                "key": f"SLPM_653.95/{off:08X}",
                "section": source_section,
                "file_offset": off,
                "source_byte_length": source_length,
                "source_ja": source_text,
                "target_ko": item["target_ko"],
                "status": item.get("status", "translated_manual_ps2_system"),
                "confidence": item.get("confidence", "high"),
            })
            occupied_offsets.add(off)
    out: list[dict] = []
    stats: Counter = Counter()
    for row in rows:
        target = row.get("target_ko") or ""
        if not target:
            stats[f"unchanged_{row.get('status', 'unknown')}"] += 1
            if require_complete and row.get("status") == "needs_translation":
                raise ValueError(f"required system translation is still empty: {row['key']}")
            continue
        off = int(row["file_offset"])
        source = row["source_ja"].encode("cp932")
        expected_len = int(row["source_byte_length"])
        if len(source) != expected_len:
            raise ValueError(f"system source length metadata mismatch: {row['key']}")
        if raw[off : off + expected_len] != source:
            raise ValueError(f"system expected-source mismatch: {row['key']}")
        out.append(row)
        stats[f"replacement_{row.get('status', 'target')}"] += 1
    return out, stats


def patch_system_strings(raw: bytes, rows: list[dict], code_map) -> tuple[bytes, list[dict]]:
    out = bytearray(raw)
    report = []
    occupied: list[tuple[int, int, str]] = []
    for row in sorted(rows, key=lambda item: int(item["file_offset"])):
        off = int(row["file_offset"])
        source_len = int(row["source_byte_length"])
        capacity = system_slot_capacity(raw, off, source_len)
        encoded = encode_korean_text(row["target_ko"], code_map)
        if len(encoded) > capacity:
            raise ValueError(
                f"system Korean text exceeds fixed slot: {row['key']} size={len(encoded)} capacity={capacity}"
            )
        region_end = off + capacity + 1
        if occupied and off < occupied[-1][1]:
            raise ValueError(f"overlapping system patch region: {row['key']} and {occupied[-1][2]}")
        occupied.append((off, region_end, row["key"]))
        out[off:region_end] = b"\0" * (region_end - off)
        out[off : off + len(encoded)] = encoded
        report.append(
            {
                "key": row["key"],
                "file_offset": off,
                "source_ja": row["source_ja"],
                "target_ko": row["target_ko"],
                "source_length": source_len,
                "slot_capacity": capacity,
                "encoded_length": len(encoded),
            }
        )
    return bytes(out), report


def reserve_codes_outside_replaced_text(
    afs: AfsArchive,
    replacements: dict[tuple[int, int], str],
    iso_path: Path,
) -> set[int]:
    fields: list[bytes] = []
    for entry in afs.entries:
        scx = ScxFile.parse(afs.read(entry))
        for command in scx.commands:
            key = (entry.index, command.text_id) if command.tag == b"tX" else None
            if key in replacements:
                # Preserve tag/id bytes, but translated Japanese prose itself no longer
                # consumes a Japanese glyph slot in the resulting build.
                fields.extend(command.fields[:2])
                fields.extend(command.fields[3:])
            else:
                fields.extend(command.fields)
    used = collect_used_sjis_codes(fields)

    # SCRIPT.AFS is not the only consumer of the shared kanji font. Until the
    # system executable and ETC text are rebuilt by later product stages, keep
    # every known Japanese string from those sources intact.
    scan = json.loads(SYSTEM_SCAN_PATH.read_text(encoding="utf-8"))
    used.update(
        collect_used_sjis_codes(
            row["text"].encode("cp932", errors="ignore") for row in scan["records"]
        )
    )
    etc = Ps2Pak(read_iso_file(iso_path, "/ETC.PAK;1"))
    used.update(
        collect_used_sjis_codes(etc.read(entry) for entry in etc.entries if entry.name.lower().endswith(".txt"))
    )
    return used


def build_replacements(
    afs: AfsArchive,
    scenario_doc: dict,
    require_complete: bool,
) -> tuple[dict[tuple[int, int], str], Counter]:
    records = scenario_doc["records"]
    by_key = {row["key"]: row for row in records}
    by_id = {(int(row["afs_index"]), int(row["local_id"])): row for row in records}
    if len(by_id) != len(records):
        raise ValueError("scenario.json contains duplicate afs_index/local_id pairs")

    replacements: dict[tuple[int, int], str] = {}
    stats: Counter = Counter()
    seen: set[tuple[int, int]] = set()
    for entry in afs.entries:
        scx = ScxFile.parse(afs.read(entry))
        scx.validate_local_text_ids()
        for command in scx.tx_commands():
            assert command.text_id is not None and command.text is not None
            key = (entry.index, command.text_id)
            row = by_id.get(key)
            if row is None:
                raise ValueError(f"scenario.json missing tX record: afs={entry.index} id={command.text_id}")
            seen.add(key)
            actual_source = command.text.decode("cp932")
            if actual_source != row["source_ja"]:
                raise ValueError(
                    f"scenario source mismatch at {row['key']}: json={row['source_ja']!r} scx={actual_source!r}"
                )
            target, method = effective_translation(row, by_key)
            if target is not None:
                replacements[key] = target
                stats[f"replacement_{method}"] += 1
            else:
                stats[f"unchanged_{row.get('status', 'unknown')}"] += 1
                if require_complete and row.get("status") == "needs_translation":
                    raise ValueError(f"required scenario translation is still empty: {row['key']}")
    if seen != set(by_id):
        extra = sorted(set(by_id) - seen)[:10]
        raise ValueError(f"scenario.json contains records not present in SCRIPT.AFS: {extra}")
    return replacements, stats


def brighten_pak_textures(pak_bytes: bytes, amount: int) -> tuple[bytes, dict]:
    """Brighten all PVR-bearing members of one fixed-size PAKFILE archive."""

    if amount == 0:
        return pak_bytes, {"amount": 0, "members": 0, "pvr_chunks": 0}
    pak = Ps2Pak(pak_bytes)
    replacements: dict[str, bytes] = {}
    changed_chunks = 0
    changed_members = 0
    for entry in pak.entries:
        payload = pak.read(entry)
        rebuilt, count = brighten_texture_container(payload, amount)
        if not count:
            continue
        replacements[entry.name] = rebuilt
        changed_chunks += count
        changed_members += 1
    patched = pak.repack_fixed_size(replacements, alignment=0x800) if replacements else pak_bytes
    return patched, {
        "amount": amount,
        "members": changed_members,
        "pvr_chunks": changed_chunks,
        "archive_sha256_before": sha256_bytes(pak_bytes),
        "archive_sha256_after": sha256_bytes(patched),
    }


def color_grade_pak_textures(pak_bytes: bytes, settings: dict[str, float | int]) -> tuple[bytes, dict]:
    """Apply one archive-specific grade to all PVR-bearing members."""

    pak = Ps2Pak(pak_bytes)
    replacements: dict[str, bytes] = {}
    changed_chunks = 0
    changed_members = 0
    for entry in pak.entries:
        payload = pak.read(entry)
        rebuilt, count = color_grade_texture_container(payload, **settings)
        if not count:
            continue
        replacements[entry.name] = rebuilt
        changed_chunks += count
        changed_members += 1
    patched = pak.repack_fixed_size(replacements, alignment=0x800) if replacements else pak_bytes
    return patched, {
        **settings,
        "members": changed_members,
        "pvr_chunks": changed_chunks,
        "archive_sha256_before": sha256_bytes(pak_bytes),
        "archive_sha256_after": sha256_bytes(patched),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PS2 ISO from assets/translation/scenario.json")
    parser.add_argument(
        "--font",
        type=Path,
        default=ROOT.parent.parent / "reference_pretendard" / "packages" / "pretendard" / "dist" / "public" / "static" / "Pretendard-Regular.otf",
        help="Hangul font used for PS2 24x24 2bpp kanji cells",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "digicarr_fantasy_excellent_kr_reuse.iso",
    )
    parser.add_argument("--require-complete-scenario", action="store_true")
    parser.add_argument("--require-complete-system", action="store_true")
    parser.add_argument("--require-complete-credits", action="store_true")
    parser.add_argument("--alignment", type=lambda x: int(x, 0), default=0x800)
    parser.add_argument(
        "--game-brightness",
        type=int,
        default=0,
        help="legacy fixed RGB offset applied to game PVR textures only; MOV files are excluded",
    )
    parser.add_argument(
        "--game-color-grade",
        choices=("none", "natural"),
        default="none",
        help="archive-aware brightness/gamma/contrast/saturation grade; MOV files are excluded",
    )
    args = parser.parse_args()
    if not -255 <= args.game_brightness <= 255:
        raise ValueError("--game-brightness must be between -255 and 255")
    if args.game_brightness and args.game_color_grade != "none":
        raise ValueError("--game-brightness and --game-color-grade cannot be combined")

    iso_path = verify_required_inputs()
    font_path = args.font.resolve()
    if not font_path.is_file():
        raise SystemExit(f"missing font: {font_path}")
    if args.alignment != 0x800:
        raise ValueError("product build currently requires original-compatible 0x800 AFS member alignment")

    scenario_doc = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    script_original = read_iso_file(iso_path, "/SCRIPT.AFS;1")
    etc_original = read_iso_file(iso_path, "/ETC.PAK;1")
    bg_original = read_iso_file(iso_path, "/BG.PAK;1")
    event_original = read_iso_file(iso_path, "/EVENT.PAK;1")
    face_original = read_iso_file(iso_path, "/FACE.PAK;1")
    slpm_original = read_iso_file(iso_path, "/SLPM_653.95;1")
    afs = AfsArchive(script_original)
    etc = Ps2Pak(etc_original)
    bg = Ps2Pak(bg_original)

    replacements, scenario_stats = build_replacements(afs, scenario_doc, args.require_complete_scenario)
    product_replacement_count = len(replacements)
    developer_tx_plan, developer_tx_stats = build_developer_tx_plan(
        scenario_doc,
        require_complete=True,
    )
    overlap = set(replacements) & set(developer_tx_plan)
    if overlap:
        raise ValueError(f"developer tX plan overlaps product replacements: {sorted(overlap)[:12]}")
    replacements = {**replacements, **developer_tx_plan}
    auxiliary_plan, auxiliary_stats = build_auxiliary_plan(
        afs,
        scenario_doc,
        require_complete=True,
    )
    system_rows, system_stats = load_system_replacements(slpm_original, args.require_complete_system)
    credit_plan, credit_stats = load_credit_plan(args.require_complete_credits)
    replacement_texts = (
        list(replacements.values())
        + [target for entry_plan in auxiliary_plan.values() for _, target in entry_plan.values()]
        + [row["target_ko"] for row in system_rows]
        + list(credit_plan["target_texts"])
    )
    # The Korean name input requires an arithmetic prefix containing the
    # supported composition syllables and jamo.  All user-facing scenario and
    # system text has been translated, so obsolete Japanese level-1 glyphs may
    # be reused; intentional CJK glyphs in Korean targets are relocated by the
    # allocator/font patcher.
    code_map = allocate_name_input_code_map(replacement_texts)
    first_level = set(first_level_kanji_codes())
    reserved_first_level: set[int] = set()
    allocated_codes = set(code_map.char_to_code.values())

    by_entry: dict[int, dict[int, str]] = {}
    for (entry_index, text_id), text in replacements.items():
        by_entry.setdefault(entry_index, {})[text_id] = text

    scx_replacements: dict[int, bytes] = {}
    member_report: list[dict] = []
    for entry in afs.entries:
        text_map = by_entry.get(entry.index, {})
        auxiliary_map = auxiliary_plan.get(entry.index, {})
        if not text_map and not auxiliary_map:
            continue
        scx = ScxFile.parse(afs.read(entry))
        tx_by_id = {command.text_id: command for command in scx.tx_commands()}
        encoded = {}
        for text_id, text in sorted(text_map.items()):
            command = tx_by_id[text_id]
            assert command.text is not None
            encoded[text_id] = (command.text, encode_korean_text(text, code_map))
        rebuilt = scx.rewrite_tx(encoded) if encoded else scx.data
        if auxiliary_map:
            reparsed = ScxFile.parse(rebuilt)
            encoded_auxiliary = {
                key: (expected_source, encode_korean_text(target_text, code_map))
                for key, (expected_source, target_text) in auxiliary_map.items()
            }
            rebuilt = reparsed.rewrite_fields(encoded_auxiliary)
        scx_replacements[entry.index] = rebuilt
        member_report.append(
            {
                "index": entry.index,
                "name": entry.name,
                "translated_messages": len(text_map),
                "translated_auxiliary_strings": len(auxiliary_map),
                "old_size": entry.size,
                "new_size": len(rebuilt),
                "delta": len(rebuilt) - entry.size,
                "old_sha256": sha256_bytes(afs.read(entry)),
                "new_sha256": sha256_bytes(rebuilt),
            }
        )

    script_patched = afs.repack_fixed_size(scx_replacements, alignment=args.alignment)
    if len(script_patched) != len(script_original):
        raise ValueError("SCRIPT.AFS fixed-size build changed outer archive length")
    patched_afs = AfsArchive(script_patched)
    decoded_verified = 0
    auxiliary_roundtrip_verified = 0
    for entry in patched_afs.entries:
        reparsed_scx = ScxFile.parse(patched_afs.read(entry))
        text_map = by_entry.get(entry.index, {})
        auxiliary_map = auxiliary_plan.get(entry.index, {})
        tx_by_id = {command.text_id: command for command in reparsed_scx.tx_commands()}
        for text_id, expected_text in text_map.items():
            actual_raw = tx_by_id[text_id].text
            assert actual_raw is not None
            actual_text = decode_korean_text(actual_raw, code_map)
            expected_normalized = normalize_text(expected_text)
            if actual_text != expected_normalized:
                raise ValueError(
                    f"SCX custom-code roundtrip mismatch: {entry.index}:{entry.name}/tX/{text_id} "
                    f"expected={expected_normalized!r} actual={actual_text!r}"
                )
            decoded_verified += 1
        if auxiliary_map:
            commands_by_index = {command.index: command for command in reparsed_scx.commands}
            for (command_index, field_index), (_, expected_text) in auxiliary_map.items():
                command = commands_by_index[command_index]
                actual_raw = command.fields[field_index]
                actual_text = decode_korean_text(actual_raw, code_map)
                expected_normalized = normalize_text(expected_text)
                if actual_text != expected_normalized:
                    raise ValueError(
                        f"SCX auxiliary custom-code roundtrip mismatch: "
                        f"{entry.index}:{entry.name}/command/{command_index}/field/{field_index} "
                        f"expected={expected_normalized!r} actual={actual_text!r}"
                    )
                auxiliary_roundtrip_verified += 1

    # Require every unchanged payload to remain byte-identical even if its AFS
    # physical offset moved due to a preceding translated member growing.
    changed_indices = set(scx_replacements)
    for old, new in zip(afs.entries, patched_afs.entries, strict=True):
        if old.index not in changed_indices and afs.read(old) != patched_afs.read(new):
            raise ValueError(f"unmodified AFS payload changed: {old.index}:{old.name}")

    kanji_original = etc.read("kanji.fon")
    kanji_patched = patch_kanji_font(kanji_original, code_map, font_path)
    credit_payloads = encode_credit_payloads(credit_plan, code_map)
    image_payloads: dict[str, bytes] = {}
    if ETC_IMAGE_READY_DIR.is_dir():
        for png_path in sorted(ETC_IMAGE_READY_DIR.glob("*.png")):
            entry_name = png_path.with_suffix(".pvr").name
            if entry_name not in etc.by_name:
                raise ValueError(f"manual ETC image has no matching PAK entry: {png_path.name}")
            original_pvr = etc.read(entry_name)
            header = parse_pvr_header(original_pvr)
            with Image.open(png_path) as source_image:
                image = source_image.convert("RGBA")
            # df_i_p1 is edited in the human-readable orientation, while the
            # game's PVR stores this texture transposed (equivalent to a 90°
            # rotation plus mirror).  Apply the same orientation convention as
            # localize_etc_preview_text_v2.py before encoding it for PS2.
            if png_path.name == "df_i_p1.png":
                image = image.transpose(Image.Transpose.TRANSPOSE)
            expected_size = (header.width, header.height)
            if image.size != expected_size:
                scale_x = image.width / header.width
                scale_y = image.height / header.height
                if scale_x != scale_y or scale_x < 1 or not float(scale_x).is_integer():
                    raise ValueError(
                        f"manual ETC image size mismatch is not an integer upscale: "
                        f"{png_path.name} expected={expected_size} actual={image.size}"
                    )
                # Keep the user's manually edited high-resolution PNG untouched;
                # normalize only the in-memory encoder input to the source PVR size.
                image = image.resize(expected_size, Image.Resampling.LANCZOS)
            image_payloads[entry_name] = encode_pvr_like(original_pvr, image)
    etc_replacements = {"kanji.fon": kanji_patched, **credit_payloads, **image_payloads}
    etc_patched = etc.repack_fixed_size(etc_replacements, alignment=0x800)
    if len(etc_patched) != len(etc_original):
        raise ValueError("ETC.PAK font/credit patch changed outer archive length")
    patched_etc = Ps2Pak(etc_patched)
    for old_entry, new_entry in zip(etc.entries, patched_etc.entries, strict=True):
        if old_entry.name not in etc_replacements and etc.read(old_entry) != patched_etc.read(new_entry):
            raise ValueError(f"unmodified ETC.PAK payload changed: {old_entry.name}")

    bg_image_payloads: dict[str, bytes] = {}
    if BG_IMAGE_READY_DIR.is_dir():
        for png_path in sorted(BG_IMAGE_READY_DIR.glob("*.png")):
            entry_name = png_path.with_suffix(".dat").name
            if entry_name not in bg.by_name:
                raise ValueError(f"Korean BG image has no matching BG.PAK entry: {png_path.name}")
            with Image.open(png_path) as source_image:
                image = source_image.convert("RGBA")
            if image.size == (800, 600):
                image = image.resize((640, 480), Image.Resampling.LANCZOS)
            elif image.size != (640, 480):
                raise ValueError(
                    f"Korean BG image must be 800x600 or 640x480: {png_path.name}={image.size}"
                )
            bg_image_payloads[entry_name] = encode_five_tile_screen_like(bg.read(entry_name), image)
    bg_patched = bg.patch_same_size(bg_image_payloads)
    patched_bg = Ps2Pak(bg_patched)
    for old_entry, new_entry in zip(bg.entries, patched_bg.entries, strict=True):
        if old_entry.name not in bg_image_payloads and bg.read(old_entry) != patched_bg.read(new_entry):
            raise ValueError(f"unmodified BG.PAK payload changed: {old_entry.name}")

    brightness_report: dict[str, dict] = {}
    color_grade_report: dict[str, dict] = {}
    event_patched = event_original
    face_patched = face_original
    if args.game_color_grade == "natural":
        etc_patched, color_grade_report["ETC.PAK"] = color_grade_pak_textures(
            etc_patched, NATURAL_GAME_GRADE["ETC.PAK"]
        )
        bg_patched, color_grade_report["BG.PAK"] = color_grade_pak_textures(
            bg_patched, NATURAL_GAME_GRADE["BG.PAK"]
        )
        event_patched, color_grade_report["EVENT.PAK"] = color_grade_pak_textures(
            event_original, NATURAL_GAME_GRADE["EVENT.PAK"]
        )
        face_patched, color_grade_report["FACE.PAK"] = color_grade_pak_textures(
            face_original, NATURAL_GAME_GRADE["FACE.PAK"]
        )
        patched_etc = Ps2Pak(etc_patched)
        patched_bg = Ps2Pak(bg_patched)
    elif args.game_brightness:
        etc_patched, brightness_report["ETC.PAK"] = brighten_pak_textures(
            etc_patched, args.game_brightness
        )
        bg_patched, brightness_report["BG.PAK"] = brighten_pak_textures(
            bg_patched, args.game_brightness
        )
        event_patched, brightness_report["EVENT.PAK"] = brighten_pak_textures(
            event_original, args.game_brightness
        )
        face_patched, brightness_report["FACE.PAK"] = brighten_pak_textures(
            face_original, args.game_brightness
        )
        patched_etc = Ps2Pak(etc_patched)
        patched_bg = Ps2Pak(bg_patched)

    slpm_patched, system_report = patch_system_strings(slpm_original, system_rows, code_map)
    slpm_patched, name_input_report = patch_name_input(slpm_patched, code_map)
    if len(slpm_patched) != len(slpm_original):
        raise ValueError("SLPM system-string patch changed executable length")
    system_roundtrip_verified = 0
    for row in system_report:
        raw_text = slpm_patched[row["file_offset"] : row["file_offset"] + row["encoded_length"]]
        actual_text = decode_korean_text(raw_text, code_map)
        expected_normalized = normalize_text(row["target_ko"])
        if actual_text != expected_normalized:
            raise ValueError(
                f"system custom-code roundtrip mismatch: {row['key']} "
                f"expected={expected_normalized!r} actual={actual_text!r}"
            )
        system_roundtrip_verified += 1

    build_dir = ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "SCRIPT_reuse.AFS").write_bytes(script_patched)
    (build_dir / "ETC_reuse.PAK").write_bytes(etc_patched)
    (build_dir / "SLPM_653.95_reuse").write_bytes(slpm_patched)

    output_iso = args.output.resolve()
    movie_payloads: dict[str, bytes] = {}
    if MOVIE_READY_DIR.is_dir():
        for movie_path in sorted(MOVIE_READY_DIR.glob("*.SFD")):
            iso_path_name = f"/MOV/{movie_path.name.upper()};1"
            payload = movie_path.read_bytes()
            original_movie = read_iso_file(iso_path, iso_path_name)
            if len(payload) != len(original_movie):
                raise ValueError(
                    f"movie replacement size mismatch: {movie_path.name} "
                    f"expected={len(original_movie)} actual={len(payload)}"
                )
            movie_payloads[iso_path_name] = payload

    iso_replacements = {
        "/SCRIPT.AFS;1": script_patched,
        "/ETC.PAK;1": etc_patched,
        "/BG.PAK;1": bg_patched,
        "/EVENT.PAK;1": event_patched,
        "/FACE.PAK;1": face_patched,
        "/SLPM_653.95;1": slpm_patched,
        **movie_payloads,
    }
    iso_members = patch_iso_members(
        iso_path,
        output_iso,
        iso_replacements,
    )
    if read_iso_file(output_iso, "/SCRIPT.AFS;1") != script_patched:
        raise ValueError("built ISO SCRIPT.AFS readback mismatch")
    if read_iso_file(output_iso, "/ETC.PAK;1") != etc_patched:
        raise ValueError("built ISO ETC.PAK readback mismatch")
    if read_iso_file(output_iso, "/BG.PAK;1") != bg_patched:
        raise ValueError("built ISO BG.PAK readback mismatch")
    if read_iso_file(output_iso, "/EVENT.PAK;1") != event_patched:
        raise ValueError("built ISO EVENT.PAK readback mismatch")
    if read_iso_file(output_iso, "/FACE.PAK;1") != face_patched:
        raise ValueError("built ISO FACE.PAK readback mismatch")
    if read_iso_file(output_iso, "/SLPM_653.95;1") != slpm_patched:
        raise ValueError("built ISO SLPM readback mismatch")
    for iso_path_name, payload in movie_payloads.items():
        if read_iso_file(output_iso, iso_path_name) != payload:
            raise ValueError(f"built ISO movie readback mismatch: {iso_path_name}")

    moved = []
    for old, new in zip(afs.entries, patched_afs.entries, strict=True):
        if old.offset != new.offset:
            moved.append(
                {
                    "index": old.index,
                    "name": old.name,
                    "old_offset": old.offset,
                    "new_offset": new.offset,
                    "size": new.size,
                }
            )

    status_counts = Counter(row.get("status", "") for row in scenario_doc["records"])
    report = {
        "ok": True,
        "local_ai_vlm_ocr_used": False,
        "source_iso": {"path": str(iso_path), "size": iso_path.stat().st_size, "sha256": sha256_file(iso_path)},
        "output_iso": {"path": str(output_iso), "size": output_iso.stat().st_size, "sha256": sha256_file(output_iso)},
        "scenario": {
            "total_rows": len(scenario_doc["records"]),
            "product_effective_translated_rows": product_replacement_count,
            "effective_translated_rows": len(replacements),
            "roundtrip_verified_rows": decoded_verified,
            "status_counts": dict(status_counts),
            "build_stats": dict(scenario_stats),
            "changed_scx_members": len(scx_replacements),
            "members": member_report,
        },
        "scenario_auxiliary": {
            **auxiliary_stats,
            "roundtrip_verified_rows": auxiliary_roundtrip_verified,
        },
        "developer_tx": developer_tx_stats,
        "credits": {
            "build_stats": dict(credit_stats),
            "patched_files": [
                {
                    "name": name,
                    "old_size": etc.by_name[name].size,
                    "new_size": len(payload),
                    "old_offset": etc.by_name[name].offset,
                    "new_offset": patched_etc.by_name[name].offset,
                }
                for name, payload in credit_payloads.items()
            ],
        },
        "etc_images": {
            "source_directory": str(ETC_IMAGE_READY_DIR),
            "patched_files": [
                {
                    "name": name,
                    "size": len(payload),
                    "sha256": sha256_bytes(payload),
                }
                for name, payload in sorted(image_payloads.items())
            ],
        },
        "bg_images": {
            "source_directory": str(BG_IMAGE_READY_DIR),
            "patched_files": [
                {"name": name, "size": len(payload), "sha256": sha256_bytes(payload)}
                for name, payload in sorted(bg_image_payloads.items())
            ],
            "original_sha256": sha256_bytes(bg_original),
            "patched_sha256": sha256_bytes(bg_patched),
        },
        "movies": {
            "source_directory": str(MOVIE_READY_DIR),
            "patched_files": [
                {"iso_path": name, "size": len(payload), "sha256": sha256_bytes(payload)}
                for name, payload in sorted(movie_payloads.items())
            ],
            "brightness_adjusted": False,
            "color_grade_adjusted": False,
        },
        "game_brightness": {
            "rgb_offset": args.game_brightness,
            "movie_excluded": True,
            "archives": brightness_report,
        },
        "game_color_grade": {
            "preset": args.game_color_grade,
            "movie_excluded": True,
            "archives": color_grade_report,
        },
        "system": {
            "effective_translated_strings": len(system_rows),
            "roundtrip_verified_strings": system_roundtrip_verified,
            "build_stats": dict(system_stats),
            "strings": system_report,
            "original_sha256": sha256_bytes(slpm_original),
            "patched_sha256": sha256_bytes(slpm_patched),
        },
        "name_input": name_input_report,
        "font": {
            "source": str(font_path),
            "source_sha256": sha256_file(font_path),
            "format": f"{KANJI_CELL_WIDTH}x{KANJI_CELL_HEIGHT} 2bpp / {KANJI_CELL_BYTES} bytes",
            "allocated_hangul": len(code_map.char_to_code),
            "reserved_first_level_codes": len(reserved_first_level),
            "free_first_level_before_hangul": len(first_level - reserved_first_level),
            "free_first_level_after_hangul": len(first_level - reserved_first_level - allocated_codes),
            "kanji_font_original_sha256": sha256_bytes(kanji_original),
            "kanji_font_patched_sha256": sha256_bytes(kanji_patched),
        },
        "afs": {
            "alignment": args.alignment,
            "size": len(script_patched),
            "names_offset": patched_afs.names_offset,
            "moved_entries": moved,
        },
        "iso_members": iso_members,
        "hangul_code_map": [
            {"char": ch, "unicode": f"U+{ord(ch):04X}", "sjis_code": f"{code:04X}"}
            for ch, code in sorted(code_map.char_to_code.items())
        ],
    }
    report_path = build_dir / "reuse_build_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "output_iso": report["output_iso"],
                "effective_translated_rows": len(replacements),
                "system_translated_strings": len(system_rows),
                "credit_translated_lines": len(credit_plan["translated00"]) + len(credit_plan["translated01"]),
                "allocated_hangul": len(code_map.char_to_code),
                "reserved_first_level_codes": len(reserved_first_level),
                "free_after_hangul": report["font"]["free_first_level_after_hangul"],
                "changed_scx_members": len(scx_replacements),
                "moved_afs_entries": len(moved),
                "bg_images": len(bg_image_payloads),
                "etc_images": len(image_payloads),
                "movies": len(movie_payloads),
                "report": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
