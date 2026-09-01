from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import io
import struct
from typing import Iterable


SECTOR_SIZE = 0x800


@dataclass(frozen=True)
class Entry:
    name: str
    offset: int
    size: int
    index: int

    @property
    def end(self) -> int:
        return self.offset + self.size


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)


class DataTopPak:
    """Parser for the Windows DATA$TOP container used by DigiCarr_Win/data.pak.

    The header count includes one terminal/root slot that overlaps the start of
    file data. Therefore the number of actual file records is header_count - 1.
    Each real record is 0x40 bytes: 48-byte name + 4 little-endian u32 values.
    The first two u32 values are duplicate relative offsets in observed files,
    followed by size and flags.
    """

    MAGIC = b"DATA$TOP"
    HEADER_SIZE = 0x40
    RECORD_SIZE = 0x40

    def __init__(self, data: bytes):
        if data[:8] != self.MAGIC:
            raise ValueError("not a DATA$TOP container")
        header_count = struct.unpack_from("<I", data, 0x38)[0]
        if header_count < 1:
            raise ValueError("invalid DATA$TOP count")
        self._data = data
        self.header_count = header_count
        self.file_count = header_count - 1
        self.data_offset = self.HEADER_SIZE + self.file_count * self.RECORD_SIZE
        if self.data_offset > len(data):
            raise ValueError("DATA$TOP table exceeds file")
        entries: list[Entry] = []
        for i in range(self.file_count):
            rec_off = self.HEADER_SIZE + i * self.RECORD_SIZE
            rec = data[rec_off : rec_off + self.RECORD_SIZE]
            raw_name = rec[:48].split(b"\0", 1)[0]
            # Most DATA$TOP names are ASCII, but the shipped Windows archive
            # contains at least one CP932 filename (e.g. byte pair 82 8c).
            # Decode with the archive's native Japanese code page instead of
            # rejecting an otherwise valid record.
            name = raw_name.decode("cp932")
            rel1, rel2, size, flags = struct.unpack_from("<4I", rec, 48)
            if rel1 != rel2:
                raise ValueError(f"DATA$TOP offset mirror mismatch at {i}: {name}")
            if flags != 0:
                raise ValueError(f"unexpected DATA$TOP flags at {i}: {name}={flags:#x}")
            abs_off = self.data_offset + rel1
            if abs_off + size > len(data):
                raise ValueError(f"DATA$TOP entry exceeds file: {name}")
            entries.append(Entry(name=name, offset=abs_off, size=size, index=i))
        self.entries = entries
        self.by_name = {e.name: e for e in entries}
        if len(self.by_name) != len(entries):
            raise ValueError("duplicate DATA$TOP names")

    @classmethod
    def from_path(cls, path: Path) -> "DataTopPak":
        return cls(path.read_bytes())

    def read(self, entry: Entry | str) -> bytes:
        e = self.by_name[entry] if isinstance(entry, str) else entry
        return self._data[e.offset : e.end]


class Ps2Pak:
    """Parser for Excellent's PAKFILE containers.

    Records are 0x40 bytes. The final two big-endian u32 fields are DVD-sector
    offset and byte size. The first two metadata dwords are zero in all surveyed
    target containers and are checked accordingly.
    """

    MAGIC = b"PAKFILE\0"
    HEADER_SIZE = 0x10
    RECORD_SIZE = 0x40

    def __init__(self, data: bytes):
        if data[:8] != self.MAGIC:
            raise ValueError("not a PAKFILE container")
        self._data = data
        self.file_count = struct.unpack_from(">I", data, 8)[0]
        table_end = self.HEADER_SIZE + self.file_count * self.RECORD_SIZE
        if table_end > len(data):
            raise ValueError("PAKFILE table exceeds file")
        entries: list[Entry] = []
        for i in range(self.file_count):
            rec_off = self.HEADER_SIZE + i * self.RECORD_SIZE
            rec = data[rec_off : rec_off + self.RECORD_SIZE]
            name = rec[:48].split(b"\0", 1)[0].decode("ascii")
            zero1, zero2, sector, size = struct.unpack_from(">4I", rec, 48)
            if zero1 or zero2:
                raise ValueError(f"unexpected PAKFILE metadata at {i}: {name}")
            abs_off = sector * SECTOR_SIZE
            if abs_off + size > len(data):
                raise ValueError(f"PAKFILE entry exceeds file: {name}")
            entries.append(Entry(name=name, offset=abs_off, size=size, index=i))
        self.entries = entries
        self.by_name = {e.name: e for e in entries}
        if len(self.by_name) != len(entries):
            raise ValueError("duplicate PAKFILE names")

    def read(self, entry: Entry | str) -> bytes:
        e = self.by_name[entry] if isinstance(entry, str) else entry
        return self._data[e.offset : e.end]

    def patch_same_size(self, replacements: dict[str, bytes]) -> bytes:
        """Replace PAKFILE members only when their byte lengths are unchanged."""

        out = bytearray(self._data)
        for name, payload in replacements.items():
            if name not in self.by_name:
                raise ValueError(f"PAKFILE member not found: {name}")
            entry = self.by_name[name]
            if len(payload) != entry.size:
                raise ValueError(
                    f"PAKFILE same-size patch mismatch: {name} "
                    f"expected={entry.size} actual={len(payload)}"
                )
            out[entry.offset : entry.end] = payload
        result = bytes(out)
        reparsed = Ps2Pak(result)
        if [(e.name, e.offset, e.size) for e in reparsed.entries] != [
            (e.name, e.offset, e.size) for e in self.entries
        ]:
            raise ValueError("PAKFILE same-size patch changed table metadata")
        return result

    def repack_fixed_size(self, replacements: dict[str, bytes], alignment: int = SECTOR_SIZE) -> bytes:
        """Repack PAKFILE members without changing the outer file byte length.

        Member offsets remain unchanged while their replacement still fits before
        the next occupied region.  Only members forced forward by an expanded
        predecessor move, and moved members keep DVD-sector alignment.  This is
        suitable for variable-length text members while keeping the ISO member
        itself fixed-size.
        """

        if alignment < SECTOR_SIZE or alignment % SECTOR_SIZE:
            raise ValueError("PAKFILE alignment must be a positive multiple of 0x800")
        unknown = set(replacements) - set(self.by_name)
        if unknown:
            raise ValueError(f"PAKFILE replacement member not found: {sorted(unknown)[:8]}")

        payloads = [replacements.get(entry.name, self.read(entry)) for entry in self.entries]
        placements: list[int] = []
        cursor = self.entries[0].offset
        for entry, payload in zip(self.entries, payloads, strict=True):
            if entry.offset >= cursor:
                start = entry.offset
            else:
                start = (cursor + alignment - 1) // alignment * alignment
            end = start + len(payload)
            if end > len(self._data):
                raise ValueError(
                    f"PAKFILE fixed-size repack exceeds outer file at {entry.name}: "
                    f"end={end:#x} file={len(self._data):#x}"
                )
            placements.append(start)
            cursor = end

        out = bytearray(self._data)
        # Clear only regions whose original member is changed or moved.  This
        # makes a no-op repack byte-identical and avoids unexplained padding
        # diffs elsewhere in the archive.
        for entry, payload, start in zip(self.entries, payloads, placements, strict=True):
            original_payload = self.read(entry)
            if start != entry.offset or payload != original_payload:
                out[entry.offset : entry.end] = b"\0" * entry.size
        for entry, payload, start in zip(self.entries, payloads, placements, strict=True):
            out[start : start + len(payload)] = payload
            # If a same-offset replacement shrank, clear stale bytes until the
            # original end while leaving the following alignment padding alone.
            if start == entry.offset and len(payload) < entry.size:
                out[start + len(payload) : entry.end] = b"\0" * (entry.size - len(payload))
            rec_off = self.HEADER_SIZE + entry.index * self.RECORD_SIZE
            struct.pack_into(">II", out, rec_off + 56, start // SECTOR_SIZE, len(payload))

        result = bytes(out)
        reparsed = Ps2Pak(result)
        if len(result) != len(self._data) or len(reparsed.entries) != len(self.entries):
            raise ValueError("PAKFILE fixed-size repack changed outer size or entry count")
        for old, new, payload, start in zip(
            self.entries, reparsed.entries, payloads, placements, strict=True
        ):
            if old.name != new.name or new.offset != start or new.size != len(payload):
                raise ValueError(f"PAKFILE fixed-size repack metadata mismatch: {old.name}")
            if reparsed.read(new) != payload:
                raise ValueError(f"PAKFILE fixed-size repack payload mismatch: {old.name}")
        return result


class AfsArchive:
    """Minimal CRI AFS parser with the 48-byte filename table used by SCRIPT.AFS."""

    MAGIC = b"AFS\0"
    NAME_RECORD_SIZE = 48

    def __init__(self, data: bytes):
        if data[:4] != self.MAGIC:
            raise ValueError("not an AFS archive")
        self._data = data
        self.file_count = struct.unpack_from("<I", data, 4)[0]
        table_end = 8 + self.file_count * 8
        if table_end + 8 > len(data):
            raise ValueError("AFS table exceeds file")
        names_offset, names_size = struct.unpack_from("<II", data, table_end)
        if names_offset + names_size > len(data):
            raise ValueError("AFS name table exceeds file")
        if names_size < self.file_count * self.NAME_RECORD_SIZE:
            raise ValueError("AFS name table is too small")
        self.names_offset = names_offset
        self.names_size = names_size
        entries: list[Entry] = []
        for i in range(self.file_count):
            off, size = struct.unpack_from("<II", data, 8 + i * 8)
            name_off = names_offset + i * self.NAME_RECORD_SIZE
            name = data[name_off : name_off + self.NAME_RECORD_SIZE].split(b"\0", 1)[0].decode("ascii")
            if off + size > len(data):
                raise ValueError(f"AFS entry exceeds file: {name}")
            entries.append(Entry(name=name, offset=off, size=size, index=i))
        self.entries = entries
        # SCRIPT.AFS contains intentional duplicate piyo_* names. Preserve them.
        self.by_name: dict[str, list[Entry]] = {}
        for e in entries:
            self.by_name.setdefault(e.name, []).append(e)

    def read(self, entry: Entry) -> bytes:
        return self._data[entry.offset : entry.end]

    def patch_entries_in_place(self, replacements: dict[int, bytes]) -> bytes:
        """Patch AFS members without moving their starting offsets.

        A replacement may grow into the member's existing padding, but it may
        not overlap the next member or the filename table.  This keeps the AFS
        file size and every unaffected member offset unchanged, which is ideal
        for first-stage ISO PoC builds.
        """

        out = bytearray(self._data)
        for index, payload in sorted(replacements.items()):
            if not (0 <= index < len(self.entries)):
                raise ValueError(f"AFS replacement index out of range: {index}")
            entry = self.entries[index]
            limit = (
                self.entries[index + 1].offset
                if index + 1 < len(self.entries)
                else self.names_offset
            )
            capacity = limit - entry.offset
            if len(payload) > capacity:
                raise ValueError(
                    f"AFS replacement does not fit existing slot: {entry.name} "
                    f"size={len(payload)} capacity={capacity}"
                )
            out[entry.offset : entry.offset + len(payload)] = payload
            # Clear stale bytes that used to belong to a longer member.  Bytes
            # beyond the old member end are padding and are left untouched.
            if len(payload) < entry.size:
                out[entry.offset + len(payload) : entry.end] = b"\0" * (entry.size - len(payload))
            struct.pack_into("<I", out, 8 + index * 8 + 4, len(payload))

        result = bytes(out)
        reparsed = AfsArchive(result)
        if len(reparsed.entries) != len(self.entries):
            raise ValueError("AFS patch changed entry count")
        for old, new in zip(self.entries, reparsed.entries, strict=True):
            if old.name != new.name or old.offset != new.offset:
                raise ValueError(f"AFS patch changed member identity/offset: {old.name}")
        return result

    def repack_fixed_size(self, replacements: dict[int, bytes], alignment: int = 0x400) -> bytes:
        """Repack members before the existing filename table without growing AFS.

        Original member offsets are retained whenever possible.  If an expanded
        member collides with the next original offset, only the necessary later
        members move forward to the requested alignment.  The filename table,
        archive byte length, entry order, and member names remain unchanged.
        """

        if alignment <= 0 or alignment & (alignment - 1):
            raise ValueError("AFS alignment must be a positive power of two")
        unknown = set(replacements) - set(range(len(self.entries)))
        if unknown:
            raise ValueError(f"AFS replacement index out of range: {sorted(unknown)[:8]}")

        payloads = [replacements.get(e.index, self.read(e)) for e in self.entries]
        placements: list[int] = []
        cursor = self.entries[0].offset
        for entry, payload in zip(self.entries, payloads, strict=True):
            if entry.offset >= cursor:
                start = entry.offset
            else:
                start = (cursor + alignment - 1) & ~(alignment - 1)
            end = start + len(payload)
            if end > self.names_offset:
                raise ValueError(
                    f"AFS fixed-size repack reaches filename table at {entry.name}: "
                    f"end={end:#x} names={self.names_offset:#x}"
                )
            placements.append(start)
            cursor = end

        out = bytearray(self._data)
        data_start = self.entries[0].offset
        out[data_start : self.names_offset] = b"\0" * (self.names_offset - data_start)
        for entry, payload, start in zip(self.entries, payloads, placements, strict=True):
            out[start : start + len(payload)] = payload
            struct.pack_into("<II", out, 8 + entry.index * 8, start, len(payload))

        result = bytes(out)
        reparsed = AfsArchive(result)
        if len(result) != len(self._data):
            raise ValueError("AFS fixed-size repack changed archive length")
        if len(reparsed.entries) != len(self.entries):
            raise ValueError("AFS fixed-size repack changed entry count")
        for old, new, payload, start in zip(
            self.entries, reparsed.entries, payloads, placements, strict=True
        ):
            if old.name != new.name or new.offset != start or new.size != len(payload):
                raise ValueError(f"AFS fixed-size repack metadata mismatch: {old.name}")
            if reparsed.read(new) != payload:
                raise ValueError(f"AFS fixed-size repack payload mismatch: {old.name}")
        return result


@dataclass(frozen=True)
class ScxLayout:
    pointer_table_offset: int
    index_table_offset: int
    stream_offset: int
    pointers: tuple[int, ...]
    command_indices: tuple[int, ...]


def parse_scx_layout(data: bytes) -> ScxLayout:
    """Parse the structural tables preceding an Excellent .scx command stream.

    Surveyed files use:
      u32 pointer_table_offset (always 8)
      u32 index_table_offset
      u32 absolute pointers from pointer_table_offset to index_table_offset
      u16 command-index table from index_table_offset to stream_offset
      tokenized command stream starting at pointers[0]

    There is one more pointer than u16 command-index value. Text-only rewrites can
    therefore keep the index table unchanged and shift absolute pointers by the
    byte deltas introduced before each pointed-to command.
    """

    if len(data) < 12:
        raise ValueError("SCX too small")
    ptr_off, idx_off = struct.unpack_from("<II", data, 0)
    if ptr_off != 8:
        raise ValueError(f"unexpected SCX pointer table offset: {ptr_off:#x}")
    if not (8 <= ptr_off < idx_off <= len(data)):
        raise ValueError("invalid SCX table offsets")
    if (idx_off - ptr_off) % 4:
        raise ValueError("unaligned SCX pointer table")
    raw_ptr_count = (idx_off - ptr_off) // 4
    raw_pointers = struct.unpack_from(f"<{raw_ptr_count}I", data, ptr_off)
    if len(raw_pointers) < 2 or raw_pointers[-1] != 0:
        raise ValueError("SCX pointer table is missing its zero sentinel")
    pointers = raw_pointers[:-1]
    stream_off = pointers[0]
    if not (idx_off <= stream_off <= len(data)):
        raise ValueError("invalid SCX stream offset")
    if (stream_off - idx_off) % 2:
        raise ValueError("unaligned SCX index table")
    idx_count = (stream_off - idx_off) // 2
    indices = struct.unpack_from(f"<{idx_count}H", data, idx_off) if idx_count else ()
    if len(pointers) != len(indices):
        raise ValueError(
            f"SCX table cardinality mismatch: pointers={len(pointers)} indices={len(indices)}"
        )
    # The first pointer is the command-stream start. Remaining pointers are
    # absolute branch/label destinations and are not globally monotonic.
    if any(p < stream_off or p > len(data) for p in pointers):
        raise ValueError("SCX pointer outside command stream")
    return ScxLayout(ptr_off, idx_off, stream_off, tuple(pointers), tuple(indices))


def read_iso_file(iso_path: Path, iso_member: str) -> bytes:
    """Read one ISO9660 file through pycdlib without materializing the whole disc."""

    try:
        import pycdlib
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("pycdlib is required") from exc
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
        out = io.BytesIO()
        iso.get_file_from_iso_fp(out, iso_path=iso_member)
        return out.getvalue()
    finally:
        iso.close()


def iter_cp932_strings(data: bytes, min_chars: int = 1) -> Iterable[tuple[int, int, str]]:
    """Yield NUL-terminated CP932 strings that contain Japanese characters.

    This is a survey helper, not a completeness claim. SCX translation extraction
    uses command-aware parsing instead of this heuristic.
    """

    start = 0
    for end, value in enumerate(data):
        if value != 0:
            continue
        if end > start:
            raw = data[start:end]
            try:
                text = raw.decode("cp932")
            except UnicodeDecodeError:
                text = ""
            if len(text) >= min_chars and any(
                "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u9fff" for ch in text
            ):
                yield start, end, text
        start = end + 1
