"""Execute the real MIPS glyph converters with Unicorn (no PCSX2 required)."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".deps/build"))
import unicorn as u
from unicorn.mips_const import (
    UC_MIPS_REG_SP, UC_MIPS_REG_RA, UC_MIPS_REG_A0,
    UC_MIPS_REG_A1, UC_MIPS_REG_A2,
)
from build_translation_iso import verify_required_inputs
from font_codec import patch_ascii_glyph_padding, kanji_index_for_code
from formats import Ps2Pak, read_iso_file


def verify() -> dict:
    source = verify_required_inputs()
    original = read_iso_file(source, "/SLPM_653.95;1")
    patched, _ = patch_ascii_glyph_padding(original)
    # A changed input routine must not silently accept the binary patch.
    try:
        patch_ascii_glyph_padding(patched)
    except ValueError:
        pass
    else:
        raise AssertionError("modified padding loop accepted as original")
    report = json.loads((ROOT / "build/reuse_build_report.json").read_text(encoding="utf-8"))
    etc = Ps2Pak(read_iso_file(Path(report["output_iso"]["path"]), "/ETC.PAK;1"))
    codes = {r["char"]: int(r["sjis_code"], 16) for r in report["hangul_code_map"]}
    start = kanji_index_for_code(codes["기"]) * 144
    seed = etc.read("kanji.fon")[start:start + 144]
    ascii_data = etc.read("ascii.fon")
    assert len(ascii_data) == 96 * 96
    palette = bytes((1, 31, 0, 0, 1, 0, 31, 0, 1, 0, 0, 31))
    colors = (0, 0xFC00, 0x83E0, 0x801F)
    scratch = 0x2192C0

    def machine(binary):
        uc = u.Uc(u.UC_ARCH_MIPS, u.UC_MODE_MIPS64 | u.UC_MODE_LITTLE_ENDIAN)
        uc.mem_map(0x100000, 0x400000)
        uc.mem_write(0x100000, binary[0x1000:])
        uc.mem_write(0x301000, palette)
        uc.reg_write(UC_MIPS_REG_SP, 0x4FF000)
        return uc

    def run(uc, address, data):
        uc.mem_write(0x300000, data)
        for register, value in ((UC_MIPS_REG_A0, 0x300000), (UC_MIPS_REG_A1, 0x301000),
                                (UC_MIPS_REG_A2, 0), (UC_MIPS_REG_RA, 0x4F0000)):
            uc.reg_write(register, value)
        uc.emu_start(address, 0x4F0000, count=100000)
        return struct.unpack("<576H", uc.mem_read(scratch, 1152))

    old = machine(original)
    run(old, 0x128B60, seed)
    dirty_space = run(old, 0x128CA8, bytes(96))
    residual = sum(dirty_space[y * 24 + x] != 0 for y in range(24) for x in range(16, 24))
    assert residual > 0 and all(dirty_space[y * 24 + x] == 0 for y in range(24) for x in range(16))
    fixed = machine(patched)
    # Check the complete current Hangul/Jamo population with both converters,
    # including the right eight columns which were stale only for ASCII.
    full_width_verified = 0
    font_data = etc.read("kanji.fon")
    for ch, code in codes.items():
        if not ("가" <= ch <= "힣" or "\u3131" <= ch <= "\u3163"):
            continue
        offset = kanji_index_for_code(code) * 144
        raw = font_data[offset:offset + 144]
        expected = tuple(colors[(byte >> shift) & 3] for byte in raw for shift in (6, 4, 2, 0))
        assert run(old, 0x128B60, raw) == run(fixed, 0x128B60, raw) == expected, ch
        full_width_verified += 1
    for index in range(96):
        # Worst case: every pixel of the reused scratch buffer is nonzero.
        fixed.mem_write(scratch - 4, b"\xCC" * 1160)
        raw = ascii_data[index * 96:(index + 1) * 96]
        pixels = run(fixed, 0x128CA8, raw)
        expected = [colors[(byte >> shift) & 3] for byte in raw for shift in (6, 4, 2, 0)]
        for y in range(24):
            assert list(pixels[y * 24:y * 24 + 16]) == expected[y * 16:(y + 1) * 16], index
            assert all(value == 0 for value in pixels[y * 24 + 16:(y + 1) * 24]), index
        assert fixed.mem_read(scratch - 4, 4) == b"\xCC" * 4
        assert fixed.mem_read(scratch + 1152, 4) == b"\xCC" * 4
    run(fixed, 0x128B60, seed)
    assert not any(run(fixed, 0x128CA8, bytes(96)))
    return {"ok": True, "original_space_residual_pixels": residual,
            "patched_space_residual_pixels": 0, "ascii_glyphs_verified": 96,
            "full_width_glyphs_verified": full_width_verified,
            "palette_and_visible_pixels_preserved": True, "buffer_canaries_preserved": True}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
