from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from formats import AfsArchive
from scx import ScxFile


ROOT = Path(__file__).resolve().parents[1]
AUX_PATH = ROOT / "assets" / "translation" / "scenario_auxiliary.json"
WINDOWS_SCENARIO_DIR = ROOT / "assets" / "extraction" / "windows" / "scenario"
JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
LEADING_RE = re.compile(r"^[ \t\u3000]*")


def _load_aux() -> dict[str, Any]:
    return json.loads(AUX_PATH.read_text(encoding="utf-8"))


def _scenario_windows_refs(scenario_doc: dict[str, Any]) -> dict[tuple[int, int], tuple[str, int]]:
    refs: dict[tuple[int, int], tuple[str, int]] = {}
    for row in scenario_doc["records"]:
        source = row.get("suggestion_source") or {}
        windows_file = source.get("windows_file")
        windows_line = source.get("windows_line")
        if windows_file and windows_line:
            refs[(int(row["afs_index"]), int(row["local_id"]))] = (
                str(windows_file),
                int(windows_line),
            )
    return refs


def _windows_lines() -> dict[str, list[str]]:
    return {
        path.name: path.read_text(encoding="utf-8", errors="replace").splitlines()
        for path in WINDOWS_SCENARIO_DIR.glob("*.txt")
    }


def _pc_choice_parts(line: str) -> list[str] | None:
    stripped = line.strip()
    lower = stripped.lower()
    if not (lower.startswith("tx,") or stripped.startswith("X,")):
        return None
    payload = stripped.split(",", 1)[1]
    if payload.endswith(","):
        payload = payload[:-1]
    parts = []
    for part in payload.replace("\\\\", "\n").split("\n"):
        # Choice buffers are often emitted twice, and one copy in the source
        # occasionally has an accidental trailing comma.  That comma is not
        # displayed, so normalize it before deduplicating candidates.
        normalized = part.strip(" \t\u3000").rstrip(",")
        parts.append(normalized)
    return parts


def _preserve_choice_indent(source: str, target: str) -> str:
    prefix = LEADING_RE.match(source).group(0)
    return prefix + target.strip(" \t\u3000")


def build_auxiliary_plan(
    afs: AfsArchive,
    scenario_doc: dict[str, Any],
    *,
    require_complete: bool = True,
) -> tuple[dict[int, dict[tuple[int, int], tuple[bytes, str]]], dict[str, Any]]:
    """Build translations for displayed SCX fields outside normal ``tX`` text.

    Windows Korean source is reused first for PS2 ``tH`` choice groups whenever
    the surrounding translated ``tX`` anchors identify exactly one choice
    buffer.  Explicit fallback rows are used only when the Windows build does
    not contain the PS2-only choice/title or when the source has no safe anchor.
    """

    aux = _load_aux()
    refs = _scenario_windows_refs(scenario_doc)
    pc_lines = _windows_lines()
    prompts: dict[str, dict[str, str]] = aux["prompts"]
    choice_fallbacks: dict[str, dict[str, str]] = aux["choice_fallbacks"]
    title_reuse: dict[str, str] = aux["title_reuse"]
    title_overrides: dict[str, str] = aux["title_overrides"]
    direct_titles: dict[str, str] = aux["direct_titles"]

    plan: dict[int, dict[tuple[int, int], tuple[bytes, str]]] = {}
    rows: list[dict[str, Any]] = []
    methods: Counter[str] = Counter()
    missed: list[dict[str, Any]] = []

    def add(
        entry_index: int,
        entry_name: str,
        command_index: int,
        field_index: int,
        source_raw: bytes,
        source_text: str,
        target: str,
        method: str,
    ) -> None:
        key = (command_index, field_index)
        dest = plan.setdefault(entry_index, {})
        previous = dest.get(key)
        value = (source_raw, target)
        if previous is not None and previous != value:
            raise ValueError(
                f"conflicting auxiliary SCX replacements: {entry_name} command={command_index} "
                f"field={field_index} old={previous!r} new={value!r}"
            )
        dest[key] = value
        methods[method] += 1
        rows.append(
            {
                "afs_index": entry_index,
                "scx": entry_name,
                "command_index": command_index,
                "field_index": field_index,
                "source_ja": source_text,
                "target_ko": target,
                "method": method,
            }
        )

    for entry in afs.entries:
        try:
            scx = ScxFile.parse(afs.read(entry))
        except ValueError:
            continue

        commands = scx.commands
        translated_tx = [
            command
            for command in scx.tx_commands()
            if command.text_id is not None and (entry.index, command.text_id) in refs
        ]

        # 1) Choice groups. Reuse the Windows Korean selection buffer when the
        # two nearest translated dialogue anchors identify one unambiguous menu.
        pos = 0
        while pos < len(commands):
            if commands[pos].tag != b"tH":
                pos += 1
                continue
            group = []
            while pos < len(commands) and commands[pos].tag == b"tH":
                command = commands[pos]
                if len(command.fields) >= 2:
                    try:
                        source_text = command.fields[1].decode("cp932")
                    except UnicodeDecodeError:
                        source_text = ""
                    if JP_RE.search(source_text):
                        group.append((command, source_text))
                pos += 1
            if not group:
                continue

            first_index = group[0][0].index
            last_index = group[-1][0].index
            before = [command for command in translated_tx if command.index < first_index]
            after = [command for command in translated_tx if command.index > last_index]
            before_ref = refs[(entry.index, before[-1].text_id)] if before else None
            after_ref = refs[(entry.index, after[0].text_id)] if after else None

            windows_choice: list[str] | None = None
            windows_line: int | None = None
            if before_ref and after_ref and before_ref[0] == after_ref[0]:
                filename = before_ref[0].replace(".scn", ".txt")
                lines = pc_lines.get(filename, [])
                unique_candidates: list[tuple[int, list[str]]] = []
                for line_number in range(before_ref[1] + 1, min(after_ref[1], len(lines) + 1)):
                    parts = _pc_choice_parts(lines[line_number - 1])
                    if parts is None or len(parts) != len(group):
                        continue
                    if parts not in [candidate[1] for candidate in unique_candidates]:
                        unique_candidates.append((line_number, parts))
                if len(unique_candidates) == 1:
                    windows_line, windows_choice = unique_candidates[0]

            for choice_index, (command, source_text) in enumerate(group):
                stripped_source = source_text.strip(" \t\u3000")
                if windows_choice is not None:
                    target = _preserve_choice_indent(source_text, windows_choice[choice_index])
                    add(
                        entry.index,
                        entry.name,
                        command.index,
                        1,
                        command.fields[1],
                        source_text,
                        target,
                        "windows_pc_ko",
                    )
                    rows[-1]["windows_file"] = before_ref[0]
                    rows[-1]["windows_line"] = windows_line
                    continue

                fallback = choice_fallbacks.get(stripped_source)
                if fallback is None:
                    missed.append(
                        {
                            "afs_index": entry.index,
                            "scx": entry.name,
                            "command_index": command.index,
                            "field_index": 1,
                            "source_ja": source_text,
                            "kind": "tH",
                        }
                    )
                    continue
                target = _preserve_choice_indent(source_text, fallback["target_ko"])
                add(
                    entry.index,
                    entry.name,
                    command.index,
                    1,
                    command.fields[1],
                    source_text,
                    target,
                    fallback["method"],
                )

        # 2) Scene/save titles. These exact Korean strings were taken from the
        # Windows build when available. Piyoko PS2-exclusive titles are the only
        # title rows marked direct translation.
        for command in commands:
            if command.tag != b"lT" or len(command.fields) < 2:
                continue
            try:
                source_text = command.fields[1].decode("cp932")
            except UnicodeDecodeError:
                continue
            if not JP_RE.search(source_text):
                continue
            override_key = f"{entry.name}|{source_text}"
            if override_key in title_overrides:
                target = title_overrides[override_key]
                method = "windows_pc_ko"
            elif source_text in title_reuse:
                target = title_reuse[source_text]
                method = "windows_pc_ko"
            elif source_text in direct_titles:
                target = direct_titles[source_text]
                method = "direct_translation"
            else:
                missed.append(
                    {
                        "afs_index": entry.index,
                        "scx": entry.name,
                        "command_index": command.index,
                        "field_index": 1,
                        "source_ja": source_text,
                        "kind": "lT",
                    }
                )
                continue
            add(
                entry.index,
                entry.name,
                command.index,
                1,
                command.fields[1],
                source_text,
                target,
                method,
            )

        # 3) Save confirmation strings. One of these is stored in field 0 and
        # therefore was invisible to all previous tX/system-message checks.
        for command in commands:
            for field_index, field in enumerate(command.fields):
                try:
                    source_text = field.decode("cp932")
                except UnicodeDecodeError:
                    continue
                prompt = prompts.get(source_text)
                if prompt is None:
                    continue
                add(
                    entry.index,
                    entry.name,
                    command.index,
                    field_index,
                    field,
                    source_text,
                    prompt["target_ko"],
                    prompt["method"],
                )

    if missed and require_complete:
        preview = "; ".join(
            f"{row['scx']}:{row['command_index']}:{row['field_index']}={row['source_ja']!r}"
            for row in missed[:12]
        )
        raise ValueError(f"untranslated auxiliary SCX strings remain ({len(missed)}): {preview}")

    # Ensure every Japanese-bearing non-tX display field is accounted for.
    uncovered: list[dict[str, Any]] = []
    for entry in afs.entries:
        try:
            scx = ScxFile.parse(afs.read(entry))
        except ValueError:
            continue
        planned = plan.get(entry.index, {})
        for command in scx.commands:
            if command.tag == b"tX":
                continue
            for field_index, field in enumerate(command.fields):
                try:
                    text = field.decode("cp932")
                except UnicodeDecodeError:
                    continue
                if not JP_RE.search(text):
                    continue
                if (command.index, field_index) not in planned:
                    uncovered.append(
                        {
                            "afs_index": entry.index,
                            "scx": entry.name,
                            "command_index": command.index,
                            "field_index": field_index,
                            "source_ja": text,
                        }
                    )
    if uncovered and require_complete:
        preview = "; ".join(
            f"{row['scx']}:{row['command_index']}:{row['field_index']}={row['source_ja']!r}"
            for row in uncovered[:12]
        )
        raise ValueError(
            f"Japanese auxiliary SCX fields are not covered ({len(uncovered)}): {preview}"
        )

    return plan, {
        "source": str(AUX_PATH),
        "total_replacements": len(rows),
        "changed_scx_members": len(plan),
        "methods": dict(methods),
        "untranslated": missed,
        "uncovered_japanese_fields": uncovered,
        "rows": rows,
    }


def audit_japanese_field_coverage(
    afs: AfsArchive,
    tx_replacements: dict[tuple[int, int], str],
    auxiliary_plan: dict[int, dict[tuple[int, int], tuple[bytes, str]]],
) -> dict[str, Any]:
    """Fail if any Japanese-bearing SCX field has no translation plan.

    The audit is intentionally based on the immutable Japanese SCRIPT.AFS, not
    on byte-pattern searches in the Korean result.  Korean custom font codes can
    look like Shift-JIS when interpreted out of context, while this structural
    check answers the actual completeness question: every original displayed
    Japanese field must be covered by either the normal tX plan or the generic
    auxiliary-field plan.
    """

    total = 0
    tx_total = 0
    auxiliary_total = 0
    covered = 0
    uncovered: list[dict[str, Any]] = []

    for entry in afs.entries:
        try:
            scx = ScxFile.parse(afs.read(entry))
        except ValueError:
            continue
        planned_aux = auxiliary_plan.get(entry.index, {})
        for command in scx.commands:
            for field_index, field in enumerate(command.fields):
                try:
                    text = field.decode("cp932")
                except UnicodeDecodeError:
                    continue
                if not JP_RE.search(text):
                    continue
                total += 1
                if command.tag == b"tX" and field_index == 2 and command.text_id is not None:
                    tx_total += 1
                    ok = (entry.index, command.text_id) in tx_replacements
                    kind = "tX"
                else:
                    auxiliary_total += 1
                    ok = (command.index, field_index) in planned_aux
                    kind = "auxiliary"
                if ok:
                    covered += 1
                    continue
                uncovered.append(
                    {
                        "afs_index": entry.index,
                        "scx": entry.name,
                        "command_index": command.index,
                        "field_index": field_index,
                        "kind": kind,
                        "source_ja": text,
                    }
                )

    if uncovered:
        preview = "; ".join(
            f"{row['scx']}:{row['command_index']}:{row['field_index']}={row['source_ja']!r}"
            for row in uncovered[:12]
        )
        raise ValueError(
            f"Japanese SCX field coverage incomplete ({len(uncovered)}/{total} uncovered): {preview}"
        )

    return {
        "original_japanese_fields": total,
        "tx_fields": tx_total,
        "auxiliary_fields": auxiliary_total,
        "covered_fields": covered,
        "uncovered_fields": 0,
    }


def build_developer_tx_plan(
    scenario_doc: dict[str, Any],
    *,
    require_complete: bool = True,
) -> tuple[dict[tuple[int, int], str], dict[str, Any]]:
    """Translate Japanese ``tX`` rows previously classified as developer tests.

    These rows are not part of normal game progression, but keeping them in
    Japanese makes whole-archive completeness audits ambiguous.  Reuse an
    already verified product translation when the exact Japanese source has one
    unambiguous Korean target.  Only sources with no such reusable target (or an
    ambiguous product target) use the explicit fallback table.
    """

    aux = _load_aux()
    fallbacks: dict[str, str] = aux["developer_tx_fallbacks"]

    translated_by_source: dict[str, set[str]] = {}
    for row in scenario_doc["records"]:
        target = str(row.get("target_ko") or "")
        if not target or row.get("status") == "not_product_translation":
            continue
        translated_by_source.setdefault(str(row["source_ja"]), set()).add(target)

    plan: dict[tuple[int, int], str] = {}
    rows: list[dict[str, Any]] = []
    methods: Counter[str] = Counter()
    unmatched: list[dict[str, Any]] = []

    for row in scenario_doc["records"]:
        if row.get("status") != "not_product_translation":
            continue
        source = str(row["source_ja"])
        if not JP_RE.search(source):
            continue

        reusable = translated_by_source.get(source, set())
        if len(reusable) == 1:
            target = next(iter(reusable))
            method = "exact_product_translation_reuse"
        elif source in fallbacks:
            target = fallbacks[source]
            method = "direct_translation"
        else:
            unmatched.append(
                {
                    "key": row["key"],
                    "afs_index": int(row["afs_index"]),
                    "local_id": int(row["local_id"]),
                    "source_ja": source,
                    "existing_targets": sorted(reusable),
                }
            )
            continue

        key = (int(row["afs_index"]), int(row["local_id"]))
        previous = plan.get(key)
        if previous is not None and previous != target:
            raise ValueError(
                f"conflicting developer tX replacements at {key}: {previous!r} != {target!r}"
            )
        plan[key] = target
        methods[method] += 1
        rows.append(
            {
                "key": row["key"],
                "afs_index": key[0],
                "local_id": key[1],
                "source_ja": source,
                "target_ko": target,
                "method": method,
            }
        )

    if unmatched and require_complete:
        preview = "; ".join(
            f"{row['key']}={row['source_ja']!r}" for row in unmatched[:12]
        )
        raise ValueError(
            f"Japanese developer/test tX strings remain untranslated ({len(unmatched)}): {preview}"
        )

    return plan, {
        "source": str(AUX_PATH),
        "total_replacements": len(rows),
        "methods": dict(methods),
        "untranslated": unmatched,
        "rows": rows,
    }
