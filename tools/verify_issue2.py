"""Read back issue #2 regressions and original colour commands from a built ISO."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".deps" / "build"))

from build_translation_iso import verify_required_inputs
from end_card import render_end_card
from font_codec import HangulCodeMap, decode_korean_text, normalize_text
from formats import AfsArchive, Ps2Pak, read_iso_file, sha256_file
from image_formats import encode_five_tile_screen_like
from scx import ScxFile


def verify(iso: Path, report_path: Path) -> dict:
    original = verify_required_inputs()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert sha256_file(iso) == report["output_iso"]["sha256"], "build report/ISO mismatch"
    code_map = HangulCodeMap({r["char"]: int(r["sjis_code"], 16) for r in report["hangul_code_map"]})
    before = AfsArchive(read_iso_file(original, "/SCRIPT.AFS;1"))
    after = AfsArchive(read_iso_file(iso, "/SCRIPT.AFS;1"))
    texts = {}
    colour_commands = 0
    for old, new in zip(before.entries, after.entries, strict=True):
        old_scx, new_scx = ScxFile.parse(before.read(old)), ScxFile.parse(after.read(new))
        assert len(old_scx.commands) == len(new_scx.commands), new.name
        for a, b in zip(old_scx.commands, new_scx.commands, strict=True):
            if a.tag == b"mc":
                assert a.fields == b.fields, (new.name, a.index, "colour changed")
                colour_commands += 1
            if b.tag == b"tX":
                text = decode_korean_text(b.text, code_map)
                texts[f"SCRIPT.AFS/{new.name}/tX/{b.text_id}"] = text
                assert "사신" not in text, (new.name, b.text_id, text)
                assert not re.search(r"(?:할|줄|갈|올|될|볼|을|릴|울|둘|킬)께|댕겅|배고[ 　]고프", text), text
    expected = {
        "hina_01.scx/tX/194": "부탁드릴게요",
        "hina_01.scx/tX/207": "할게요",
        "hina_03.scx/tX/86": "배도 고프고",
        "hina_03.scx/tX/106": "도울게",
        "hina_07.scx/tX/163": "반드시",
        "hina_09.scx/tX/101": "댕강",
        "hina_10.scx/tX/97": "잡혔지",
        "hina_10.scx/tX/147": "둘 다 바보뉴",
        "erik_06.scx/tX/131": "설거지",
        "main.scx/tX/81": "그러고 보니，다른 사람들은 어떻게 됐을까？",
        "hina_06.scx/tX/163": "히나기쿠 씨의\n어머님은",
    }
    for key, fragment in expected.items():
        assert normalize_text(fragment) in texts["SCRIPT.AFS/" + key], key
    for key in ("main.scx/tX/81", "hina_06.scx/tX/163"):
        text = texts["SCRIPT.AFS/" + key]
        assert len(text.splitlines()) == 3, key
        assert "보\n니" not in text and "씨\n의" not in text, key
        # Native advance: full-width=24, ASCII=16. Use the conservative
        # original 22-full-width-character line budget (528), not HTML width.
        assert all(sum(16 if ord(ch) < 128 else 24 for ch in line) <= 528
                   for line in text.splitlines()), key
    # Native font files remain untouched. This is a file-level check, not
    # an upscaled renderer test.
    etc_before = Ps2Pak(read_iso_file(original, "/ETC.PAK;1"))
    etc_after = Ps2Pak(read_iso_file(iso, "/ETC.PAK;1"))
    for name in ("ascii.fon", "kana.fon"):
        assert etc_before.read(name) == etc_after.read(name), name
    slpm = read_iso_file(iso, "/SLPM_653.95;1")
    assert slpm[0x128E20 - 0xFF000:0x128E28 - 0xFF000] == bytes.fromhex("000040ad040040ad")
    bg_before = Ps2Pak(read_iso_file(original, "/BG.PAK;1"))
    bg_after = Ps2Pak(read_iso_file(iso, "/BG.PAK;1"))
    font = Path(report["font"]["source"])
    expected_card = encode_five_tile_screen_like(bg_before.read("bg100.dat"), render_end_card(font))
    assert bg_after.read("bg100.dat") == expected_card, "ending card readback mismatch"
    assert bg_before.read("bg100.dat") != expected_card, "Japanese ending card unchanged"
    return {
        "ok": True,
        "iso_sha256": report["output_iso"]["sha256"],
        "scx_verified": len(after.entries),
        "colour_commands_identical_to_original": colour_commands,
        "issue_regressions_verified": len(expected),
        "native_fonts_unchanged": ["ascii.fon", "kana.fon"],
        "ending_card_readback": True,
        "ascii_padding_fix_readback": True,
        "upscaled_runtime_verified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("iso", type=Path)
    parser.add_argument("--report", type=Path, default=ROOT / "build/reuse_build_report.json")
    args = parser.parse_args()
    print(json.dumps(verify(args.iso, args.report), ensure_ascii=False, indent=2))
