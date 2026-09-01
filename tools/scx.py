from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping
import bisect
import struct

from formats import ScxLayout, parse_scx_layout


@dataclass(frozen=True)
class ScxCommand:
    index: int
    start: int
    end: int
    raw: bytes
    fields: tuple[bytes, ...]

    @property
    def tag(self) -> bytes:
        return self.fields[0] if self.fields else b""

    @property
    def text_id(self) -> int | None:
        if self.tag != b"tX" or len(self.fields) < 3:
            return None
        try:
            return int(self.fields[1].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None

    @property
    def text(self) -> bytes | None:
        if self.tag != b"tX" or len(self.fields) < 3:
            return None
        # tX is tag\0local-id\0text\0, and text can contain literal LF bytes.
        return self.fields[2]


@dataclass(frozen=True)
class ScxFile:
    data: bytes
    layout: ScxLayout
    commands: tuple[ScxCommand, ...]

    @classmethod
    def parse(cls, data: bytes) -> "ScxFile":
        layout = parse_scx_layout(data)
        commands: list[ScxCommand] = []
        pos = layout.stream_offset
        index = 0
        while pos < len(data):
            # Commands are NUL-terminated and followed by LF. A tX text payload
            # may contain LF, so LF alone cannot delimit a command.
            term = data.find(b"\x00\x0a", pos)
            if term < 0:
                raise ValueError(f"unterminated SCX command at {pos:#x}")
            end = term + 2
            raw_no_lf = data[pos : term + 1]
            fields = tuple(raw_no_lf[:-1].split(b"\0"))
            commands.append(ScxCommand(index, pos, end, data[pos:end], fields))
            pos = end
            index += 1
        if pos != len(data):
            raise ValueError("SCX parser did not consume full file")

        starts = {c.start for c in commands}
        bad = [p for p in layout.pointers if p not in starts]
        if bad:
            raise ValueError(f"SCX pointer(s) do not target command starts: {bad[:8]}")
        return cls(data, layout, tuple(commands))

    def tx_commands(self) -> tuple[ScxCommand, ...]:
        return tuple(c for c in self.commands if c.tag == b"tX")

    def validate_local_text_ids(self) -> None:
        tx = self.tx_commands()
        ids = [c.text_id for c in tx]
        if any(x is None for x in ids):
            raise ValueError("SCX tX command has a non-numeric local id")
        expected = list(range(len(ids)))
        if ids != expected:
            raise ValueError(f"SCX local text IDs are not sequential: {ids[:16]}")

    def rewrite_fields(
        self,
        replacements: Mapping[tuple[int, int], tuple[bytes, bytes]],
    ) -> bytes:
        """Replace arbitrary command fields and repair all absolute pointers.

        ``replacements`` maps ``(command_index, field_index)`` to
        ``(expected_source, replacement)``.  Unlike :meth:`rewrite_tx`, this is
        intentionally generic so PS2-only displayed strings stored in ``tH``
        choices, ``lT`` scene titles, ``YN`` prompts, or even field 0 can be
        translated without pretending they are normal dialogue.
        """

        if not replacements:
            return self.data

        by_index = {c.index: c for c in self.commands}
        new_command_raw: dict[int, bytes] = {}
        deltas: list[tuple[int, int]] = []
        per_command: dict[int, list[tuple[int, bytes, bytes]]] = {}
        for (command_index, field_index), (expected, replacement) in replacements.items():
            per_command.setdefault(command_index, []).append((field_index, expected, replacement))

        for command_index, edits in sorted(per_command.items()):
            cmd = by_index.get(command_index)
            if cmd is None:
                raise ValueError(f"SCX replacement command index not present: {command_index}")
            fields = list(cmd.fields)
            for field_index, expected, replacement in sorted(edits):
                if not 0 <= field_index < len(fields):
                    raise ValueError(
                        f"SCX replacement field index out of range: command={command_index} "
                        f"field={field_index} fields={len(fields)}"
                    )
                if fields[field_index] != expected:
                    raise ValueError(
                        f"SCX expected-source mismatch at command={command_index} field={field_index}: "
                        f"expected={expected!r} actual={fields[field_index]!r}"
                    )
                if b"\0" in replacement:
                    raise ValueError(
                        f"SCX replacement contains NUL: command={command_index} field={field_index}"
                    )
                fields[field_index] = replacement
            raw = b"\0".join(fields) + b"\0\x0a"
            new_command_raw[command_index] = raw
            deltas.append((cmd.end, len(raw) - len(cmd.raw)))

        out = bytearray(self.data[: self.layout.stream_offset])
        for cmd in self.commands:
            out.extend(new_command_raw.get(cmd.index, cmd.raw))

        ends = [x[0] for x in deltas]
        prefix: list[int] = []
        total = 0
        for _, delta in deltas:
            total += delta
            prefix.append(total)

        def shift_before(old_pos: int) -> int:
            idx = bisect.bisect_right(ends, old_pos) - 1
            return prefix[idx] if idx >= 0 else 0

        for i, old_ptr in enumerate(self.layout.pointers):
            new_ptr = old_ptr + shift_before(old_ptr)
            struct.pack_into("<I", out, self.layout.pointer_table_offset + i * 4, new_ptr)
        struct.pack_into(
            "<I",
            out,
            self.layout.pointer_table_offset + len(self.layout.pointers) * 4,
            0,
        )

        result = bytes(out)
        reparsed = ScxFile.parse(result)
        if len(reparsed.commands) != len(self.commands):
            raise ValueError("SCX field rewrite changed command population")
        replaced_field0 = {command_index for command_index, field_index in replacements if field_index == 0}
        for old_cmd, new_cmd in zip(self.commands, reparsed.commands, strict=True):
            if old_cmd.index not in replaced_field0 and old_cmd.tag != new_cmd.tag:
                raise ValueError(
                    f"SCX field rewrite changed untouched command tag at index {old_cmd.index}"
                )
        return result

    def rewrite_tx(self, replacements: Mapping[int, tuple[bytes, bytes]]) -> bytes:
        """Replace tX text by local text id and repair all absolute pointers.

        replacements maps local text id -> (expected_source_text, replacement_text).
        The command set and command order are preserved. Every changed command is
        reconstructed from its original fields, and every pointer in the SCX
        pointer table is shifted according to byte deltas before its original
        target. Expected-source validation prevents applying a mapping to the
        wrong game revision or the wrong local text id.
        """

        self.validate_local_text_ids()
        tx_by_id = {c.text_id: c for c in self.tx_commands()}
        if set(replacements) - set(tx_by_id):
            missing = sorted(set(replacements) - set(tx_by_id))
            raise ValueError(f"replacement IDs not present in SCX: {missing[:16]}")

        new_command_raw: dict[int, bytes] = {}
        deltas: list[tuple[int, int]] = []  # (old command end, delta)
        for text_id, (expected, replacement) in sorted(replacements.items()):
            cmd = tx_by_id[text_id]
            if cmd.text != expected:
                raise ValueError(
                    f"SCX expected-source mismatch for tX {text_id}: "
                    f"expected={expected!r} actual={cmd.text!r}"
                )
            if b"\0" in replacement:
                raise ValueError(f"replacement tX {text_id} contains NUL")
            fields = list(cmd.fields)
            fields[2] = replacement
            raw = b"\0".join(fields) + b"\0\x0a"
            new_command_raw[cmd.index] = raw
            deltas.append((cmd.end, len(raw) - len(cmd.raw)))

        if not deltas:
            return self.data

        out = bytearray(self.data[: self.layout.stream_offset])
        for cmd in self.commands:
            out.extend(new_command_raw.get(cmd.index, cmd.raw))

        # Convert old absolute command-start pointers to new absolute positions.
        ends = [x[0] for x in deltas]
        prefix: list[int] = []
        total = 0
        for _, delta in deltas:
            total += delta
            prefix.append(total)

        def shift_before(old_pos: int) -> int:
            # A replacement changes bytes within its command. Pointers target
            # command starts only, so all changes whose old command end <= target
            # have occurred before that target.
            idx = bisect.bisect_right(ends, old_pos) - 1
            return prefix[idx] if idx >= 0 else 0

        for i, old_ptr in enumerate(self.layout.pointers):
            new_ptr = old_ptr + shift_before(old_ptr)
            struct.pack_into("<I", out, self.layout.pointer_table_offset + i * 4, new_ptr)
        # Preserve the zero sentinel immediately after real pointers.
        struct.pack_into(
            "<I",
            out,
            self.layout.pointer_table_offset + len(self.layout.pointers) * 4,
            0,
        )

        result = bytes(out)
        # Mechanical self-check: rewritten file must still parse and retain the
        # same command/tag sequence and tX population.
        reparsed = ScxFile.parse(result)
        if [c.tag for c in reparsed.commands] != [c.tag for c in self.commands]:
            raise ValueError("SCX rewrite changed command sequence")
        if len(reparsed.tx_commands()) != len(self.tx_commands()):
            raise ValueError("SCX rewrite changed tX population")
        return result


def decode_tx_cp932(scx: ScxFile) -> Iterable[tuple[int, str]]:
    for cmd in scx.tx_commands():
        assert cmd.text_id is not None and cmd.text is not None
        yield cmd.text_id, cmd.text.decode("cp932")
