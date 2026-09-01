"""Composing Korean name-entry keyboard for the PS2 executable.

The two original kana pages become a consonant/basic-vowel page and a
consonant/compound-vowel page.  A compact MIPS hook composes the selected jamo
in the active name cell while the game's delete, field-change and confirmation
routines retain their native behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from font_codec import (
    HangulCodeMap,
    first_level_kanji_codes,
    is_hangul_jamo,
    is_hangul_syllable,
    normalize_text,
)


SLPM_TEXT_VA = 0x00100000
SLPM_TEXT_FILE_OFFSET = 0x1000
NAME_APPEND_VA = 0x0011B808
NAME_APPEND_FILE_OFFSET = NAME_APPEND_VA - SLPM_TEXT_VA + SLPM_TEXT_FILE_OFFSET
ORDINAL_TO_SJIS_VA = NAME_APPEND_VA + 8
ORDINAL_TO_SJIS_FILE_OFFSET = NAME_APPEND_FILE_OFFSET + 8
ORDINAL_TO_SJIS_CAPACITY = 0x88

STRING_TABLE_VA = 0x0020A1F0
STRING_TABLE_FILE_OFFSET = 0x0010B1F0
STRING_SLOT_SIZE = 8
STRING_SLOT_COUNT = 168
POINTER_TABLE_FILE_OFFSET = 0x000FB8E8
GRID_SLOTS_PER_PAGE = 90

# Space left after the strings used by the Korean keyboard.  The original
# kana strings in this area are no longer referenced after the pointer-table
# rewrite, so it is a stable executable-owned code cave.
PATCH_CODE_VA = STRING_TABLE_VA + 40 * STRING_SLOT_SIZE
PATCH_CODE_FILE_OFFSET = STRING_TABLE_FILE_OFFSET + 40 * STRING_SLOT_SIZE
PATCH_CODE_CAPACITY = STRING_SLOT_COUNT * STRING_SLOT_SIZE - 40 * STRING_SLOT_SIZE

INITIALS = tuple("ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ")
VOWELS = tuple("ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ")
JAMO = INITIALS + VOWELS

BASIC_VOWELS = tuple("ㅏㅑㅓㅕㅗㅛㅜㅠㅡㅣ")
COMPOUND_VOWELS = tuple(vowel for vowel in VOWELS if vowel not in BASIC_VOWELS)
PAGE1_JAMO = INITIALS + BASIC_VOWELS
PAGE2_JAMO = INITIALS + COMPOUND_VOWELS
if set(PAGE1_JAMO + PAGE2_JAMO) != set(JAMO):
    raise AssertionError("Korean composing pages do not expose every jamo")

# 받침 없음, ㄱ, ㄴ, ㄹ, ㅁ, ㅇ.  These six states cover the overwhelming
# majority of Korean personal names while keeping an arithmetic code layout.
CORE_FINALS = (0, 1, 4, 8, 16, 21)
CORE_SYLLABLE_COUNT = 19 * 21 * len(CORE_FINALS)
INPUT_SYLLABLE_COUNT = CORE_SYLLABLE_COUNT
JAMO_BASE = INPUT_SYLLABLE_COUNT


def input_syllables() -> tuple[str, ...]:
    out: list[str] = []
    for initial in range(19):
        for vowel in range(21):
            for final in CORE_FINALS:
                out.append(chr(0xAC00 + (initial * 21 + vowel) * 28 + final))
    if len(out) != INPUT_SYLLABLE_COUNT or len(set(out)) != len(out):
        raise AssertionError("invalid Korean input syllable layout")
    return tuple(out)


def allocate_name_input_code_map(texts: Iterable[str]) -> HangulCodeMap:
    """Reserve the arithmetic composition range, then every build character."""

    ordered: list[str] = list(input_syllables()) + list(JAMO)
    seen = set(ordered)
    normalized = [normalize_text(text) for text in texts]

    # Preserve all Korean prose characters not already covered by the input
    # alphabet.  Sorting makes builds deterministic.
    extras = sorted(
        {
            ch
            for text in normalized
            for ch in text
            if is_hangul_syllable(ch) and ch not in seen
        }
    )
    ordered.extend(extras)
    seen.update(extras)

    text_jamo = sorted({ch for text in normalized for ch in text if is_hangul_jamo(ch) and ch not in seen})
    ordered.extend(text_jamo)
    seen.update(text_jamo)

    # Level-1 Japanese glyphs that occur intentionally in Korean target text
    # must be relocated because the arithmetic prefix occupies their original
    # cells.  patch_kanji_font copies their original bitmaps to the new cells.
    level1 = set(first_level_kanji_codes())
    relocated: list[str] = []
    for ch in sorted({ch for text in normalized for ch in text}):
        if ch in seen:
            continue
        try:
            raw = ch.encode("cp932")
        except UnicodeEncodeError:
            continue
        if len(raw) == 2 and int.from_bytes(raw, "big") in level1:
            relocated.append(ch)
    ordered.extend(relocated)

    codes = first_level_kanji_codes()
    if len(ordered) > len(codes):
        raise ValueError(
            "Korean name-input/font allocation exceeds level-1 font: "
            f"need={len(ordered)} available={len(codes)}"
        )
    return HangulCodeMap(dict(zip(ordered, codes[: len(ordered)], strict=True)))


def _reg(name: str) -> int:
    regs = {
        "zero": 0, "v0": 2, "v1": 3, "a0": 4, "a1": 5, "a2": 6, "a3": 7,
        "t0": 8, "t1": 9, "t2": 10, "t3": 11, "t4": 12, "t5": 13,
        "t6": 14, "t7": 15, "s0": 16, "s1": 17, "s2": 18, "s3": 19,
        "s4": 20, "s5": 21, "s6": 22, "s7": 23, "t8": 24, "t9": 25,
        "gp": 28, "sp": 29, "fp": 30, "ra": 31,
    }
    return regs[name]


@dataclass
class _Fixup:
    offset: int
    kind: str
    label: str


class MipsBuilder:
    """Tiny label-aware assembler for the integer subset used by the hook."""

    def __init__(self, base_va: int):
        self.base_va = base_va
        self.words: list[int] = []
        self.labels: dict[str, int] = {}
        self.fixups: list[_Fixup] = []

    @property
    def pc(self) -> int:
        return self.base_va + len(self.words) * 4

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"duplicate label: {name}")
        self.labels[name] = self.pc

    def word(self, value: int) -> None:
        self.words.append(value & 0xFFFFFFFF)

    def r(self, funct: int, rd: str, rs: str = "zero", rt: str = "zero", shamt: int = 0) -> None:
        self.word((_reg(rs) << 21) | (_reg(rt) << 16) | (_reg(rd) << 11) | (shamt << 6) | funct)

    def i(self, op: int, rt: str, rs: str, imm: int) -> None:
        self.word((op << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | (imm & 0xFFFF))

    def addiu(self, rt: str, rs: str, imm: int) -> None: self.i(0x09, rt, rs, imm)
    def sltiu(self, rt: str, rs: str, imm: int) -> None: self.i(0x0B, rt, rs, imm)
    def andi(self, rt: str, rs: str, imm: int) -> None: self.i(0x0C, rt, rs, imm)
    def ori(self, rt: str, rs: str, imm: int) -> None: self.i(0x0D, rt, rs, imm)
    def lui(self, rt: str, imm: int) -> None: self.i(0x0F, rt, "zero", imm)
    def lw(self, rt: str, off: int, base: str) -> None: self.i(0x23, rt, base, off)
    def lbu(self, rt: str, off: int, base: str) -> None: self.i(0x24, rt, base, off)
    def sw(self, rt: str, off: int, base: str) -> None: self.i(0x2B, rt, base, off)
    def sb(self, rt: str, off: int, base: str) -> None: self.i(0x28, rt, base, off)
    def addu(self, rd: str, rs: str, rt: str) -> None: self.r(0x21, rd, rs, rt)
    def subu(self, rd: str, rs: str, rt: str) -> None: self.r(0x23, rd, rs, rt)
    def sll(self, rd: str, rt: str, shamt: int) -> None: self.r(0x00, rd, "zero", rt, shamt)
    def srl(self, rd: str, rt: str, shamt: int) -> None: self.r(0x02, rd, "zero", rt, shamt)
    def or_(self, rd: str, rs: str, rt: str) -> None: self.r(0x25, rd, rs, rt)
    def divu(self, rs: str, rt: str) -> None: self.r(0x1B, "zero", rs, rt)
    def mflo(self, rd: str) -> None: self.r(0x12, rd)
    def mfhi(self, rd: str) -> None: self.r(0x10, rd)
    def jr(self, rs: str) -> None: self.word((_reg(rs) << 21) | 0x08)
    def nop(self) -> None: self.word(0)
    def move(self, rd: str, rs: str) -> None: self.addu(rd, rs, "zero")

    def branch(self, op: int, rs: str, rt: str, label: str) -> None:
        off = len(self.words)
        self.word((op << 26) | (_reg(rs) << 21) | (_reg(rt) << 16))
        self.fixups.append(_Fixup(off, "branch", label))

    def beq(self, rs: str, rt: str, label: str) -> None: self.branch(0x04, rs, rt, label)
    def bne(self, rs: str, rt: str, label: str) -> None: self.branch(0x05, rs, rt, label)
    def beqz(self, rs: str, label: str) -> None: self.beq(rs, "zero", label)
    def bnez(self, rs: str, label: str) -> None: self.bne(rs, "zero", label)

    def jump(self, op: int, label: str) -> None:
        off = len(self.words)
        self.word(op << 26)
        self.fixups.append(_Fixup(off, "jump", label))

    def j(self, label: str) -> None: self.jump(0x02, label)
    def jal(self, label: str) -> None: self.jump(0x03, label)

    def j_abs(self, target: int) -> None:
        self.word((0x02 << 26) | ((target >> 2) & 0x03FFFFFF))

    def jal_abs(self, target: int) -> None:
        self.word((0x03 << 26) | ((target >> 2) & 0x03FFFFFF))

    def li(self, rt: str, value: int) -> None:
        if -0x8000 <= value <= 0x7FFF:
            self.addiu(rt, "zero", value)
        elif 0 <= value <= 0xFFFF:
            self.ori(rt, "zero", value)
        else:
            self.lui(rt, (value + 0x8000) >> 16)
            self.addiu(rt, rt, value & 0xFFFF)

    def absolute(self, rt: str, value: int) -> None:
        self.lui(rt, (value + 0x8000) >> 16)
        self.addiu(rt, rt, value & 0xFFFF)

    def finish(self) -> bytes:
        words = self.words[:]
        for fix in self.fixups:
            target = self.labels[fix.label]
            pc = self.base_va + fix.offset * 4
            if fix.kind == "branch":
                delta = (target - (pc + 4)) // 4
                if not -0x8000 <= delta <= 0x7FFF:
                    raise ValueError(f"branch out of range: {fix.label}")
                words[fix.offset] |= delta & 0xFFFF
            else:
                words[fix.offset] |= (target >> 2) & 0x03FFFFFF
        return b"".join(word.to_bytes(4, "little") for word in words)


def _build_patch_code() -> bytes:
    """Build the compact composition routine placed after keyboard strings."""

    a = MipsBuilder(PATCH_CODE_VA)
    # Stack: 0=id, 4=last pointer, 8=count pointer, 12=ordinal, 28=caller ra.
    a.label("entry")
    a.addiu("sp", "sp", -32)
    # Blank cells have a null string pointer.  Return without touching the
    # active name buffer; keep the ra save in the branch delay slot so the
    # hook still fits the 1,024-byte executable code cave exactly.
    a.beqz("a1", "return")
    a.sw("ra", 28, "sp")
    a.absolute("t0", STRING_TABLE_VA)
    a.subu("t1", "a1", "t0")
    a.srl("t1", "t1", 3)
    a.sw("t1", 0, "sp")
    a.sltiu("v0", "t1", len(JAMO))
    a.beqz("v0", "append_selected")
    a.nop()

    # Locate active 11-byte name buffer and its character count.
    a.lw("t2", 8, "a0")
    a.sll("t3", "t2", 2)
    a.addu("t3", "a0", "t3")
    a.addiu("t3", "t3", 12)
    a.sw("t3", 8, "sp")
    a.lw("t4", 0, "t3")
    a.beqz("t4", "append_selected")
    a.nop()
    a.sll("t5", "t2", 3)
    a.sll("t6", "t2", 1)
    a.addu("t5", "t5", "t6")
    a.addu("t5", "t5", "t2")
    a.addu("t5", "t5", "a0")
    a.addiu("t5", "t5", 24)
    a.addiu("t4", "t4", -1)
    a.sll("t4", "t4", 1)
    a.addu("t5", "t5", "t4")
    a.sw("t5", 4, "sp")

    # Convert the last custom Shift-JIS code to its level-1 ordinal.
    a.lbu("a2", 0, "t5")
    a.lbu("a3", 1, "t5")
    a.jal("sjis_to_ordinal")
    a.nop()
    a.li("t0", JAMO_BASE)
    a.subu("t2", "v0", "t0")
    a.sltiu("t3", "t2", len(JAMO))
    a.beqz("t3", "last_is_syllable")
    a.nop()

    # Pending initial + vowel -> a complete LV syllable.
    a.sltiu("t3", "t2", len(INITIALS))
    a.beqz("t3", "append_selected")
    a.nop()
    a.lw("t1", 0, "sp")
    a.sltiu("t3", "t1", len(INITIALS))
    a.bnez("t3", "append_selected")
    a.nop()
    a.addiu("t1", "t1", -len(INITIALS))
    a.sll("t3", "t2", 4)
    a.sll("t4", "t2", 2)
    a.addu("t3", "t3", "t4")
    a.addu("t3", "t3", "t2")  # initial * 21
    a.addu("t3", "t3", "t1")
    a.sll("a2", "t3", 2)
    a.sll("t4", "t3", 1)
    a.addu("a2", "a2", "t4")  # (L*21+V)*6
    a.j("replace_with_ordinal")
    a.nop()

    a.label("last_is_syllable")
    a.sltiu("t0", "v0", INPUT_SYLLABLE_COUNT)
    a.beqz("t0", "append_selected")
    a.nop()
    a.li("t0", len(CORE_FINALS))
    a.divu("v0", "t0")
    a.mflo("t6")  # LV index
    a.mfhi("t7")  # compact final slot
    a.label("decoded_syllable")
    a.lw("t1", 0, "sp")
    a.sltiu("t0", "t1", len(INITIALS))
    a.beqz("t0", "new_vowel")
    a.nop()
    a.bnez("t7", "append_selected")
    a.nop()
    # Map an initial-key id to a supported final slot.
    for key_id, slot in ((0, 1), (2, 2), (5, 3), (6, 4), (11, 5)):
        a.li("t0", key_id)
        a.beq("t1", "t0", f"apply_core_final_{slot}")
        a.nop()
    a.j("append_selected")
    a.nop()

    for slot in range(1, 6):
        a.label(f"apply_core_final_{slot}")
        a.sll("a2", "t6", 2)
        a.sll("t0", "t6", 1)
        a.addu("a2", "a2", "t0")
        a.addiu("a2", "a2", slot)
        a.j("replace_with_ordinal")
        a.nop()

    a.label("new_vowel")
    a.beqz("t7", "append_selected")
    a.nop()
    # Split the supported final into the next syllable's initial.
    a.lw("t0", 8, "sp")
    a.lw("t2", 0, "t0")
    a.sltiu("t3", "t2", 5)
    a.beqz("t3", "return")
    a.nop()
    # final slot -> next initial index
    for marker, initial in ((1, 0), (2, 2), (3, 5), (4, 6), (5, 11)):
        a.li("t0", marker)
        a.beq("t7", "t0", f"split_final_{marker}")
        a.nop()
    a.j("append_selected")
    a.nop()
    for marker, initial in ((1, 0), (2, 2), (3, 5), (4, 6), (5, 11)):
        a.label(f"split_final_{marker}")
        a.li("t3", initial)
        a.j("split_apply")
        a.nop()

    a.label("split_apply")
    # Replace previous syllable with its no-final form.
    a.sw("t3", 12, "sp")
    a.sll("a2", "t6", 2)
    a.sll("t0", "t6", 1)
    a.addu("a2", "a2", "t0")
    a.jal_abs(ORDINAL_TO_SJIS_VA)
    a.nop()
    a.lw("t5", 4, "sp")
    a.srl("t0", "v0", 8)
    a.sb("t0", 0, "t5")
    a.sb("v0", 1, "t5")
    # Append new LV made from the split final and selected vowel.
    a.lw("t3", 12, "sp")
    a.lw("t1", 0, "sp")
    a.addiu("t1", "t1", -len(INITIALS))
    a.sll("t0", "t3", 4)
    a.sll("t2", "t3", 2)
    a.addu("t0", "t0", "t2")
    a.addu("t0", "t0", "t3")
    a.addu("t0", "t0", "t1")
    a.sll("a2", "t0", 2)
    a.sll("t2", "t0", 1)
    a.addu("a2", "a2", "t2")
    a.jal_abs(ORDINAL_TO_SJIS_VA)
    a.nop()
    a.lw("t5", 4, "sp")
    a.addiu("t5", "t5", 2)
    a.srl("t0", "v0", 8)
    a.sb("t0", 0, "t5")
    a.sb("v0", 1, "t5")
    a.sb("zero", 2, "t5")
    a.lw("t0", 8, "sp")
    a.lw("t1", 0, "t0")
    a.addiu("t1", "t1", 1)
    a.sw("t1", 0, "t0")
    a.j("return")
    a.nop()

    a.label("replace_with_ordinal")
    a.jal_abs(ORDINAL_TO_SJIS_VA)
    a.nop()
    a.lw("t5", 4, "sp")
    a.srl("t0", "v0", 8)
    a.sb("t0", 0, "t5")
    a.sb("v0", 1, "t5")
    a.j("return")
    a.nop()

    a.label("append_selected")
    a.lw("t2", 8, "a0")
    a.sll("t3", "t2", 2)
    a.addu("t3", "a0", "t3")
    a.addiu("t3", "t3", 12)
    a.lw("t4", 0, "t3")
    a.sltiu("t0", "t4", 5)
    a.beqz("t0", "return")
    a.nop()
    a.sll("t5", "t2", 3)
    a.sll("t6", "t2", 1)
    a.addu("t5", "t5", "t6")
    a.addu("t5", "t5", "t2")
    a.addu("t5", "t5", "a0")
    a.addiu("t5", "t5", 24)
    a.sll("t6", "t4", 1)
    a.addu("t5", "t5", "t6")
    a.lbu("t0", 0, "a1")
    a.lbu("t1", 1, "a1")
    a.sb("t0", 0, "t5")
    a.sb("t1", 1, "t5")
    a.sb("zero", 2, "t5")
    a.addiu("t4", "t4", 1)
    a.sw("t4", 0, "t3")

    a.label("return")
    a.lw("ra", 28, "sp")
    a.jr("ra")
    a.addiu("sp", "sp", 32)

    # a2=lead, a3=trail -> v0=level-1 ordinal.
    a.label("sjis_to_ordinal")
    a.sltiu("t0", "a2", 0xA0)
    a.bnez("t0", "sjis_low_lead")
    a.nop()
    a.addiu("t0", "a2", -0xC1)
    a.j("sjis_have_lead")
    a.nop()
    a.label("sjis_low_lead")
    a.addiu("t0", "a2", -0x81)
    a.label("sjis_have_lead")
    a.sll("t0", "t0", 1)
    a.sltiu("t1", "a3", 0x9F)
    a.beqz("t1", "sjis_high_trail")
    a.nop()
    a.addiu("t0", "t0", 0x21)
    a.sltiu("t1", "a3", 0x7F)
    a.beqz("t1", "sjis_trail_ge_7f")
    a.addiu("t2", "a3", -0x1F)
    a.j("sjis_have_col")
    a.nop()
    a.label("sjis_trail_ge_7f")
    a.addiu("t2", "a3", -0x20)
    a.j("sjis_have_col")
    a.nop()
    a.label("sjis_high_trail")
    a.addiu("t0", "t0", 0x22)
    a.addiu("t2", "a3", -0x7E)
    a.label("sjis_have_col")
    a.addiu("t0", "t0", -0x30)
    a.sll("v0", "t0", 6)
    a.sll("t1", "t0", 5)
    a.addu("v0", "v0", "t1")
    a.sll("t1", "t0", 1)
    a.subu("v0", "v0", "t1")  # row * 94
    a.addiu("t2", "t2", -0x21)
    a.addu("v0", "v0", "t2")
    a.jr("ra")
    a.nop()

    return a.finish()


def _build_ordinal_to_sjis_code() -> bytes:
    """Build the ordinal encoder in the reclaimed original append body."""

    a = MipsBuilder(ORDINAL_TO_SJIS_VA)
    # a2=level-1 ordinal -> v0=big-endian numeric Shift-JIS code.
    a.label("ordinal_to_sjis")
    a.li("t0", 94)
    a.divu("a2", "t0")
    a.mflo("t1")
    a.mfhi("t2")
    a.addiu("t1", "t1", 0x30)
    a.addiu("t2", "t2", 0x21)
    a.addiu("t3", "t1", -0x21)
    a.srl("t3", "t3", 1)
    a.addiu("t3", "t3", 0x81)
    a.sltiu("t4", "t3", 0xA0)
    a.bnez("t4", "ordinal_lead_done")
    a.nop()
    a.addiu("t3", "t3", 0x40)
    a.label("ordinal_lead_done")
    a.andi("t4", "t1", 1)
    a.beqz("t4", "ordinal_even_row")
    a.nop()
    a.addiu("t2", "t2", 0x1F)
    a.sltiu("t4", "t2", 0x7F)
    a.bnez("t4", "ordinal_pack")
    a.nop()
    a.addiu("t2", "t2", 1)
    a.j("ordinal_pack")
    a.nop()
    a.label("ordinal_even_row")
    a.addiu("t2", "t2", 0x7E)
    a.label("ordinal_pack")
    a.sll("v0", "t3", 8)
    a.or_("v0", "v0", "t2")
    a.jr("ra")
    a.nop()

    return a.finish()


def _pointer_for_slot(slot: int) -> int:
    return STRING_TABLE_VA + slot * STRING_SLOT_SIZE


def patch_name_input(slpm: bytes, code_map: HangulCodeMap) -> tuple[bytes, dict]:
    """Install the jamo pages and the compact in-place composition hook."""

    out = bytearray(slpm)
    region_size = STRING_SLOT_COUNT * STRING_SLOT_SIZE
    out[STRING_TABLE_FILE_OFFSET : STRING_TABLE_FILE_OFFSET + region_size] = b"\0" * region_size
    for slot, ch in enumerate(JAMO):
        encoded = code_map.char_to_code[ch].to_bytes(2, "big")
        off = STRING_TABLE_FILE_OFFSET + slot * STRING_SLOT_SIZE
        out[off : off + 2] = encoded

    patch_code = _build_patch_code()
    if len(patch_code) > PATCH_CODE_CAPACITY:
        raise ValueError(f"name composition hook is too large: {len(patch_code)} > {PATCH_CODE_CAPACITY}")
    out[PATCH_CODE_FILE_OFFSET : PATCH_CODE_FILE_OFFSET + len(patch_code)] = patch_code

    ordinal_code = _build_ordinal_to_sjis_code()
    if len(ordinal_code) > ORDINAL_TO_SJIS_CAPACITY:
        raise ValueError(
            f"ordinal encoder is too large: {len(ordinal_code)} > {ORDINAL_TO_SJIS_CAPACITY}"
        )
    jump_to_hook = (0x08000000 | ((PATCH_CODE_VA >> 2) & 0x03FFFFFF)).to_bytes(4, "little")
    out[NAME_APPEND_FILE_OFFSET : NAME_APPEND_FILE_OFFSET + 8] = jump_to_hook + b"\0\0\0\0"
    out[ORDINAL_TO_SJIS_FILE_OFFSET : ORDINAL_TO_SJIS_FILE_OFFSET + len(ordinal_code)] = ordinal_code

    # The first page exposes all consonants and simple vowels.  The second
    # repeats the consonants (so a new syllable can begin without page flips)
    # and exposes the compound vowels.  Unused grid cells remain null.
    jamo_slot = {ch: slot for slot, ch in enumerate(JAMO)}
    page0: list[int | None] = [jamo_slot[ch] for ch in PAGE1_JAMO]
    page1: list[int | None] = [jamo_slot[ch] for ch in PAGE2_JAMO]
    page0 += [None] * (GRID_SLOTS_PER_PAGE - len(page0))
    page1 += [None] * (GRID_SLOTS_PER_PAGE - len(page1))
    for index, slot in enumerate(page0 + page1):
        value = 0 if slot is None else _pointer_for_slot(slot)
        off = POINTER_TABLE_FILE_OFFSET + index * 4
        out[off : off + 4] = value.to_bytes(4, "little")

    return bytes(out), {
        "mode": "jamo_composition",
        "page1_jamo": "".join(PAGE1_JAMO),
        "page2_jamo": "".join(PAGE2_JAMO),
        "page1_count": len(PAGE1_JAMO),
        "page2_count": len(PAGE2_JAMO),
        "supported_finals": "ㄱㄴㄹㅁㅇ",
        "composable_syllables": INPUT_SYLLABLE_COUNT,
        "hook_size": len(patch_code),
        "hook_capacity": PATCH_CODE_CAPACITY,
    }
