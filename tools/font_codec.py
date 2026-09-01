from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
import unicodedata

from PIL import Image, ImageDraw, ImageFont


KANJI_CELL_WIDTH = 24
KANJI_CELL_HEIGHT = 24
KANJI_BITS_PER_PIXEL = 2
KANJI_CELL_BYTES = KANJI_CELL_WIDTH * KANJI_CELL_HEIGHT * KANJI_BITS_PER_PIXEL // 8
KANJI_STANDARD_GLYPHS = 2965
KANJI_TOTAL_GLYPHS = 3011
FIRST_LEVEL_JIS_ROW_START = 0x30
FIRST_LEVEL_JIS_ROW_FULL_END = 0x4E
FIRST_LEVEL_JIS_LAST_ROW = 0x4F
FIRST_LEVEL_JIS_LAST_COL = 0x53

# Characters present in the Windows Korean script but not directly encodable
# by Python's CP932 codec.  Use visually/functionally equivalent glyphs that
# the original PS2 renderer already supports.
TEXT_NORMALIZATION = {
    "·": "・",
    "∼": "～",
    "—": "―",
    "⋯": "…",
    # The Windows Korean localization uses IDEOGRAPHIC SPACE between nearly
    # every Korean word.  On PS2 Hangul is rendered through a full-width kanji
    # cell, while the native ASCII space provides the appropriate half-width
    # Korean word spacing and also avoids needless AFS growth.
    "\u3000": " ",
}


@dataclass(frozen=True)
class HangulCodeMap:
    char_to_code: Mapping[str, int]

    @property
    def code_to_char(self) -> dict[int, str]:
        return {code: ch for ch, code in self.char_to_code.items()}


def is_hangul_syllable(ch: str) -> bool:
    return "\uac00" <= ch <= "\ud7a3"


def is_hangul_jamo(ch: str) -> bool:
    return "\u3131" <= ch <= "\u3163"


def normalize_text(text: str) -> str:
    out: list[str] = []
    for ch in text:
        ch = TEXT_NORMALIZATION.get(ch, ch)
        if "\uf900" <= ch <= "\ufaff":
            ch = unicodedata.normalize("NFKC", ch)
        out.append(ch)
    return "".join(out)


def sjis_to_jis(code: int) -> tuple[int, int]:
    lead = (code >> 8) & 0xFF
    trail = code & 0xFF
    if not (0x81 <= lead <= 0x9F or 0xE0 <= lead <= 0xEF):
        raise ValueError(f"not a Shift-JIS double-byte lead: {code:#06x}")
    if not (0x40 <= trail <= 0xFC) or trail == 0x7F:
        raise ValueError(f"not a Shift-JIS double-byte trail: {code:#06x}")

    lead_base = 0x81 if lead <= 0x9F else 0xC1
    if trail < 0x9F:
        row = (lead - lead_base) * 2 + 0x21
        col = trail - (0x1F if trail < 0x7F else 0x20)
    else:
        row = (lead - lead_base) * 2 + 0x22
        col = trail - 0x7E
    return row, col


def jis_to_sjis(row: int, col: int) -> int:
    if not (0x21 <= row <= 0x7E and 0x21 <= col <= 0x7E):
        raise ValueError(f"invalid JIS row/col: {row:#x}/{col:#x}")
    lead = ((row - 0x21) >> 1) + 0x81
    if lead > 0x9F:
        lead += 0x40
    if row & 1:
        trail = col + 0x1F
        if trail >= 0x7F:
            trail += 1
    else:
        trail = col + 0x7E
    return (lead << 8) | trail


def first_level_kanji_codes() -> tuple[int, ...]:
    codes: list[int] = []
    for row in range(FIRST_LEVEL_JIS_ROW_START, FIRST_LEVEL_JIS_ROW_FULL_END + 1):
        for col in range(0x21, 0x7F):
            codes.append(jis_to_sjis(row, col))
    for col in range(0x21, FIRST_LEVEL_JIS_LAST_COL + 1):
        codes.append(jis_to_sjis(FIRST_LEVEL_JIS_LAST_ROW, col))
    if len(codes) != 2965:
        raise AssertionError(f"unexpected first-level kanji population: {len(codes)}")
    return tuple(codes)


def kanji_index_for_code(code: int) -> int:
    row, col = sjis_to_jis(code)
    if row < FIRST_LEVEL_JIS_ROW_START:
        raise ValueError(f"code is before kanji.fon level-1 range: {code:#06x}")
    index = (row - FIRST_LEVEL_JIS_ROW_START) * 94 + (col - 0x21)
    if not (0 <= index < 2965):
        raise ValueError(f"code is outside supported level-1 kanji range: {code:#06x}")
    return index


def iter_sjis_double_byte_codes(raw: bytes) -> Iterable[int]:
    i = 0
    while i < len(raw):
        b0 = raw[i]
        if 0x81 <= b0 <= 0x9F or 0xE0 <= b0 <= 0xEF:
            if i + 1 < len(raw):
                b1 = raw[i + 1]
                if 0x40 <= b1 <= 0xFC and b1 != 0x7F:
                    yield (b0 << 8) | b1
                    i += 2
                    continue
        i += 1


def collect_used_sjis_codes(fields: Iterable[bytes]) -> set[int]:
    used: set[int] = set()
    for raw in fields:
        used.update(iter_sjis_double_byte_codes(raw))
    return used


def collect_non_hangul_sjis_codes(texts: Iterable[str]) -> set[int]:
    """Reserve native CP932 codes intentionally kept inside Korean target text."""

    encoded_parts: list[bytes] = []
    for text in texts:
        for ch in normalize_text(text):
            if is_hangul_syllable(ch):
                continue
            try:
                encoded_parts.append(ch.encode("cp932"))
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"non-Hangul target character cannot be encoded as CP932: U+{ord(ch):04X} {ch!r}"
                ) from exc
    return collect_used_sjis_codes(encoded_parts)


def allocate_hangul_codes(texts: Iterable[str], used_sjis_codes: set[int]) -> HangulCodeMap:
    hangul = sorted({ch for text in texts for ch in normalize_text(text) if is_hangul_syllable(ch)})
    free_codes = [code for code in first_level_kanji_codes() if code not in used_sjis_codes]
    if len(hangul) > len(free_codes):
        raise ValueError(
            f"not enough unused level-1 kanji slots: need={len(hangul)} free={len(free_codes)}"
        )
    return HangulCodeMap(dict(zip(hangul, free_codes[: len(hangul)], strict=True)))


def encode_korean_text(text: str, code_map: HangulCodeMap) -> bytes:
    out = bytearray()
    for ch in normalize_text(text):
        code = code_map.char_to_code.get(ch)
        if code is not None:
            out.extend(((code >> 8) & 0xFF, code & 0xFF))
            continue
        if is_hangul_syllable(ch) or is_hangul_jamo(ch):
            if code is None:
                raise ValueError(f"Hangul glyph is not allocated: {ch}")
        try:
            out.extend(ch.encode("cp932"))
        except UnicodeEncodeError as exc:
            raise ValueError(f"character cannot be encoded for PS2 renderer: U+{ord(ch):04X} {ch!r}") from exc
    return bytes(out)


def decode_korean_text(raw: bytes, code_map: HangulCodeMap) -> str:
    """Decode a PS2 string using the build's custom Hangul/SJIS code map."""

    custom = code_map.code_to_char
    out: list[str] = []
    i = 0
    while i < len(raw):
        b0 = raw[i]
        if (0x81 <= b0 <= 0x9F or 0xE0 <= b0 <= 0xEF) and i + 1 < len(raw):
            code = (b0 << 8) | raw[i + 1]
            mapped = custom.get(code)
            if mapped is not None:
                out.append(mapped)
            else:
                out.append(raw[i : i + 2].decode("cp932"))
            i += 2
            continue
        out.append(bytes((b0,)).decode("cp932"))
        i += 1
    return "".join(out)


def render_hangul_cell(ch: str, font: ImageFont.FreeTypeFont) -> bytes:
    """Render one PS2 kanji-font cell: 24x24 pixels packed as 2bpp MSB-first."""

    if not (is_hangul_syllable(ch) or is_hangul_jamo(ch)):
        raise ValueError(f"not a Hangul syllable: {ch!r}")
    image = Image.new("L", (KANJI_CELL_WIDTH, KANJI_CELL_HEIGHT), 0)
    draw = ImageDraw.Draw(image)
    bbox = font.getbbox(ch)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = (KANJI_CELL_WIDTH - width) // 2 - bbox[0]
    y = (KANJI_CELL_HEIGHT - height) // 2 - bbox[1]
    draw.text((x, y), ch, font=font, fill=255)

    levels = [min(3, max(0, (value * 3 + 127) // 255)) for value in image.getdata()]
    packed = bytearray()
    for i in range(0, len(levels), 4):
        p0, p1, p2, p3 = levels[i : i + 4]
        packed.append((p0 << 6) | (p1 << 4) | (p2 << 2) | p3)
    if len(packed) != KANJI_CELL_BYTES:
        raise AssertionError(f"bad packed glyph size: {len(packed)}")
    return bytes(packed)


def patch_kanji_font(
    original: bytes,
    code_map: HangulCodeMap,
    font_path: Path,
    font_size: int = 22,
) -> bytes:
    if len(original) % KANJI_CELL_BYTES:
        raise ValueError(f"kanji.fon is not {KANJI_CELL_BYTES}-byte cell aligned")
    glyph_count = len(original) // KANJI_CELL_BYTES
    if glyph_count != KANJI_TOTAL_GLYPHS:
        raise ValueError(
            f"unexpected kanji.fon glyph count: expected={KANJI_TOTAL_GLYPHS} actual={glyph_count}"
        )
    font = ImageFont.truetype(str(font_path), font_size)
    out = bytearray(original)
    # Read relocation sources before overwriting any destination cell.
    relocated_cells: dict[str, bytes] = {}
    for ch in code_map.char_to_code:
        if is_hangul_syllable(ch) or is_hangul_jamo(ch):
            continue
        native = ch.encode("cp932")
        if len(native) != 2:
            raise ValueError(f"relocated font glyph is not double-byte CP932: {ch!r}")
        native_index = kanji_index_for_code(int.from_bytes(native, "big"))
        start = native_index * KANJI_CELL_BYTES
        relocated_cells[ch] = original[start : start + KANJI_CELL_BYTES]

    for ch, code in code_map.char_to_code.items():
        index = kanji_index_for_code(code)
        start = index * KANJI_CELL_BYTES
        if is_hangul_syllable(ch) or is_hangul_jamo(ch):
            cell = render_hangul_cell(ch, font)
        else:
            cell = relocated_cells[ch]
        out[start : start + KANJI_CELL_BYTES] = cell
    return bytes(out)
