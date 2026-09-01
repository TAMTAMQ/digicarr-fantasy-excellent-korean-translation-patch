from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from font_codec import HangulCodeMap, encode_korean_text


ROOT = Path(__file__).resolve().parents[1]
ETC_TEXT = ROOT / "assets" / "translation" / "etc_text.json"
WORKLIST = ROOT / "assets" / "translation" / "credits_worklist.json"


def load_credit_plan(require_complete: bool = False) -> tuple[dict, Counter]:
    etc_doc = json.loads(ETC_TEXT.read_text(encoding="utf-8"))
    work = json.loads(WORKLIST.read_text(encoding="utf-8"))
    etc_by = {row["key"]: row for row in etc_doc["records"]}
    source00 = etc_by["ETC.PAK/ending_staff00.txt"]["source_ja"].splitlines()
    source01 = etc_by["ETC.PAK/ending_staff01.txt"]["source_ja"].splitlines()

    primary_by_index = {int(row["line_index"]): row for row in work["primary_staff00"]}
    extras_by_index = {int(row["line_index"]): row for row in work["staff01_extras"]}
    aliases = {int(row["staff01_line_index"]): int(row["staff00_line_index"]) for row in work["staff01_aliases"]}

    stats: Counter = Counter()
    effective00 = list(source00)
    translated00: set[int] = set()
    target_texts: list[str] = []
    for index, row in primary_by_index.items():
        if source00[index] != row["source_ja"]:
            raise ValueError(f"credit staff00 source mismatch at line {index}")
        target = row.get("target_ko") or ""
        if target:
            effective00[index] = target
            translated00.add(index)
            target_texts.append(target)
            stats["staff00_translated_lines"] += 1
        elif row.get("needs_translation"):
            stats["staff00_untranslated_lines"] += 1
            if require_complete:
                raise ValueError(f"required credit translation is empty: {row['id']}")

    effective01 = list(source01)
    translated01: set[int] = set()
    for line01, line00 in aliases.items():
        if source01[line01] != source00[line00]:
            raise ValueError(f"credit alias source mismatch: staff01/{line01} -> staff00/{line00}")
        if line00 in translated00:
            effective01[line01] = effective00[line00]
            translated01.add(line01)
            stats["staff01_alias_translated_lines"] += 1

    for index, row in extras_by_index.items():
        if source01[index] != row["source_ja"]:
            raise ValueError(f"credit staff01 extra source mismatch at line {index}")
        target = row.get("target_ko") or ""
        if target:
            effective01[index] = target
            translated01.add(index)
            target_texts.append(target)
            stats["staff01_extra_translated_lines"] += 1
        elif row.get("needs_translation"):
            stats["staff01_extra_untranslated_lines"] += 1
            if require_complete:
                raise ValueError(f"required credit translation is empty: {row['id']}")

    return {
        "source00": source00,
        "source01": source01,
        "effective00": effective00,
        "effective01": effective01,
        "translated00": translated00,
        "translated01": translated01,
        "target_texts": target_texts,
    }, stats


def _encode_lines(source: list[str], effective: list[str], translated: set[int], code_map: HangulCodeMap) -> bytes:
    if len(source) != len(effective):
        raise ValueError("credit source/effective line count mismatch")
    encoded_lines: list[bytes] = []
    for index, (old, new) in enumerate(zip(source, effective, strict=True)):
        if index in translated:
            encoded_lines.append(encode_korean_text(new, code_map))
        else:
            encoded_lines.append(old.encode("cp932"))
    # splitlines() removes the final LF. Both shipping staff files end in LF.
    return b"\n".join(encoded_lines) + b"\n"


def encode_credit_payloads(plan: dict, code_map: HangulCodeMap) -> dict[str, bytes]:
    replacements: dict[str, bytes] = {}
    if plan["translated00"]:
        replacements["ending_staff00.txt"] = _encode_lines(
            plan["source00"], plan["effective00"], plan["translated00"], code_map
        )
    if plan["translated01"]:
        replacements["ending_staff01.txt"] = _encode_lines(
            plan["source01"], plan["effective01"], plan["translated01"], code_map
        )
    return replacements
