from __future__ import annotations

from dataclasses import dataclass
import io
import struct
from typing import Iterable

from PIL import Image, ImageEnhance


@dataclass(frozen=True)
class FlChunk:
    index: int
    tag: str
    flags: int
    offset: int
    size: int
    data: bytes


@dataclass(frozen=True)
class PvrHeader:
    pixel_format: int
    data_format: int
    width: int
    height: int
    payload_offset: int
    payload_size: int
    has_gbix: bool


PVR_PIXEL_NAMES = {
    0: "ARGB1555",
    1: "RGB565",
    2: "ARGB4444",
    3: "YUV422",
    4: "BUMP",
    5: "RGB555",
    # This game uses the extended direct-colour values below. Their observed
    # payload sizes are exactly 4 and 3 bytes/pixel respectively.
    6: "ARGB8888",
    7: "RGB888",
}

PVR_DATA_NAMES = {
    1: "TWIDDLED",
    9: "RECTANGLE",
}


def lzs3_decompress(data: bytes) -> bytes:
    """Decompress the Lzs3 wrapper used by FACE.PAK.

    Header:
      0x00  'Lzs3'
      0x04  u32 little-endian uncompressed size
      0x08  u32 little-endian compressed-stream size
      0x0c  LZSS stream

    The stream is the classic 4 KiB LZSS layout: one flag byte covers eight
    tokens, low bit first; set bit = literal, clear bit = two-byte backref.
    Backrefs use a 12-bit absolute ring position and a 4-bit length + 3.
    """

    if not data.startswith(b"Lzs3"):
        raise ValueError("not an Lzs3 stream")
    if len(data) < 12:
        raise ValueError("truncated Lzs3 header")
    raw_size, comp_size = struct.unpack_from("<II", data, 4)
    if 12 + comp_size != len(data):
        raise ValueError(
            f"Lzs3 compressed-size mismatch: header={comp_size} actual={len(data)-12}"
        )

    src = memoryview(data)[12:]
    ring = bytearray(4096)
    ring_pos = 4096 - 18
    out = bytearray()
    pos = 0

    while len(out) < raw_size:
        if pos >= len(src):
            raise ValueError("Lzs3 ended before requested output size")
        flags = src[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= raw_size:
                break
            if flags & (1 << bit):
                if pos >= len(src):
                    raise ValueError("truncated Lzs3 literal")
                value = src[pos]
                pos += 1
                out.append(value)
                ring[ring_pos] = value
                ring_pos = (ring_pos + 1) & 0xFFF
            else:
                if pos + 1 >= len(src):
                    raise ValueError("truncated Lzs3 back-reference")
                lo = src[pos]
                hi = src[pos + 1]
                pos += 2
                source_pos = lo | ((hi & 0xF0) << 4)
                length = (hi & 0x0F) + 3
                for i in range(length):
                    value = ring[(source_pos + i) & 0xFFF]
                    out.append(value)
                    ring[ring_pos] = value
                    ring_pos = (ring_pos + 1) & 0xFFF
                    if len(out) >= raw_size:
                        break

    if len(out) != raw_size:
        raise ValueError("Lzs3 output-size mismatch")
    if pos != comp_size:
        raise ValueError(f"Lzs3 did not consume full stream: {pos}/{comp_size}")
    return bytes(out)


def lzs3_compress(raw: bytes) -> bytes:
    """Compress bytes into the FACE.PAK Lzs3 wrapper.

    This uses a greedy 4 KiB-window matcher compatible with lzs3_decompress().
    It intentionally favors correctness and deterministic output over reproducing
    the original compressor byte-for-byte.
    """

    stream = bytearray()
    recent: dict[bytes, list[int]] = {}
    pos = 0
    size = len(raw)

    def remember(index: int) -> None:
        if index + 3 > size:
            return
        key = raw[index : index + 3]
        bucket = recent.setdefault(key, [])
        bucket.append(index)
        cutoff = index - 4096
        while bucket and bucket[0] < cutoff:
            del bucket[0]
        # Texture data has extremely common 3-byte prefixes (especially zero
        # runs and flat-color pixels).  Keeping only 96 recent candidates can
        # discard the older source that yields the full 18-byte LZSS match,
        # producing streams noticeably larger than the game's originals.
        # A wider bounded history stays deterministic while recovering most of
        # the original compressor's match quality without an unbounded scan.
        if len(bucket) > 4096:
            del bucket[:-4096]

    def find_match(at: int, hypothetical_source: int | None = None) -> tuple[int, int]:
        best_source = -1
        best_length = 0
        if at + 3 > size:
            return best_source, best_length
        key = raw[at : at + 3]
        candidates = recent.get(key, ())
        for source in reversed(candidates):
            distance = at - source
            if distance <= 0 or distance > 4096:
                continue
            # Lzs3/LZSS back-references may overlap the bytes currently
            # being produced.  The decoder reads and writes the ring
            # buffer one byte at a time, so matches longer than the
            # source distance are valid (and important for long runs).
            max_length = min(18, size - at)
            length = 3
            while length < max_length and raw[source + length] == raw[at + length]:
                length += 1
            if length > best_length:
                best_source = source
                best_length = length
                if length == 18:
                    return best_source, best_length
        # For one-byte lazy lookahead, the byte at the current position would
        # have been emitted as a literal and therefore becomes a valid source.
        if hypothetical_source is not None and at - hypothetical_source <= 4096:
            if raw[hypothetical_source : hypothetical_source + 3] == key:
                max_length = min(18, size - at)
                length = 3
                while (
                    length < max_length
                    and raw[hypothetical_source + length] == raw[at + length]
                ):
                    length += 1
                if length > best_length:
                    best_source = hypothetical_source
                    best_length = length
        return best_source, best_length

    while pos < size:
        flag_index = len(stream)
        stream.append(0)
        flags = 0
        for bit in range(8):
            if pos >= size:
                break

            best_source, best_length = find_match(pos)
            if best_length >= 3 and best_length < 18 and pos + 1 < size:
                _, next_length = find_match(pos + 1, hypothetical_source=pos)
                if next_length > best_length:
                    # A one-byte literal can expose a longer following match.
                    # This small lazy step recovers several bytes on tight FACE
                    # entries where pure greedy parsing crosses a sector boundary.
                    best_source = -1
                    best_length = 0

            if best_length >= 3:
                ring_source = (4096 - 18 + best_source) & 0xFFF
                stream.append(ring_source & 0xFF)
                stream.append(((ring_source >> 4) & 0xF0) | (best_length - 3))
                start = pos
                pos += best_length
                for index in range(start, pos):
                    remember(index)
            else:
                flags |= 1 << bit
                stream.append(raw[pos])
                remember(pos)
                pos += 1

        stream[flag_index] = flags

    result = b"Lzs3" + struct.pack("<II", len(raw), len(stream)) + bytes(stream)
    if lzs3_decompress(result) != raw:
        raise AssertionError("Lzs3 compressor roundtrip mismatch")
    return result


def unwrap_lzs3(data: bytes) -> bytes:
    return lzs3_decompress(data) if data.startswith(b"Lzs3") else data


def parse_fl(data: bytes) -> tuple[bytes, tuple[FlChunk, ...]]:
    """Parse the game's FL container after optional Lzs3 decompression."""

    raw = unwrap_lzs3(data)
    if raw[:4] != b"FL\0\0":
        raise ValueError("not an FL container")
    if len(raw) < 16:
        raise ValueError("truncated FL header")
    count = struct.unpack_from("<I", raw, 4)[0]
    table_end = 16 + count * 16
    if table_end > len(raw):
        raise ValueError("FL descriptor table exceeds file")

    chunks: list[FlChunk] = []
    for i in range(count):
        off = 16 + i * 16
        tag_raw = raw[off : off + 4]
        try:
            tag = tag_raw.rstrip(b"\0").decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid FL tag at {i}") from exc
        flags, size, payload_off = struct.unpack_from("<III", raw, off + 4)
        if payload_off < table_end or payload_off + size > len(raw):
            raise ValueError(
                f"FL chunk outside file: index={i} tag={tag} "
                f"offset={payload_off:#x} size={size:#x}"
            )
        chunks.append(
            FlChunk(
                index=i,
                tag=tag,
                flags=flags,
                offset=payload_off,
                size=size,
                data=raw[payload_off : payload_off + size],
            )
        )
    return raw, tuple(chunks)


def _strip_gbix(data: bytes) -> tuple[bytes, bool]:
    if not data.startswith(b"GBIX"):
        return data, False
    if len(data) < 8:
        raise ValueError("truncated GBIX header")
    gbix_payload = struct.unpack_from("<I", data, 4)[0]
    total = 8 + gbix_payload
    if total > len(data):
        raise ValueError("GBIX chunk exceeds file")
    return data[total:], True


def parse_pvr_header(data: bytes) -> PvrHeader:
    pvr, has_gbix = _strip_gbix(data)
    if len(pvr) < 16 or pvr[:4] != b"PVRT":
        raise ValueError("not a PVRT texture")
    chunk_size = struct.unpack_from("<I", pvr, 4)[0]
    if 8 + chunk_size != len(pvr):
        raise ValueError(
            f"PVRT size mismatch: header={chunk_size:#x} total={len(pvr):#x}"
        )
    pixel_format = pvr[8]
    data_format = pvr[9]
    width, height = struct.unpack_from("<HH", pvr, 12)
    if not width or not height:
        raise ValueError("zero-sized PVRT texture")
    payload_size = len(pvr) - 16
    return PvrHeader(
        pixel_format=pixel_format,
        data_format=data_format,
        width=width,
        height=height,
        payload_offset=(len(data) - len(pvr)) + 16,
        payload_size=payload_size,
        has_gbix=has_gbix,
    )


def _morton_index(x: int, y: int) -> int:
    """Return Dreamcast/PVR Morton index with X bits in even positions."""

    result = 0
    bit = 0
    while (1 << bit) <= max(x, y):
        result |= ((x >> bit) & 1) << (bit * 2)
        result |= ((y >> bit) & 1) << (bit * 2 + 1)
        bit += 1
    return result


def _detwiddle_16(payload: bytes, width: int, height: int) -> bytes:
    if width != height or width & (width - 1):
        raise ValueError("TWIDDLED decoder currently requires square power-of-two texture")
    expected = width * height * 2
    if len(payload) != expected:
        raise ValueError("TWIDDLED 16bpp payload-size mismatch")
    out = bytearray(expected)
    for y in range(height):
        row = y * width
        for x in range(width):
            src_pixel = _morton_index(x, y)
            src = src_pixel * 2
            dst = (row + x) * 2
            out[dst : dst + 2] = payload[src : src + 2]
    return bytes(out)


def _twiddle_16(payload: bytes, width: int, height: int) -> bytes:
    """Inverse of _detwiddle_16 for square 16bpp textures."""

    if width != height or width & (width - 1):
        raise ValueError("TWIDDLED encoder currently requires square power-of-two texture")
    expected = width * height * 2
    if len(payload) != expected:
        raise ValueError("TWIDDLED 16bpp payload-size mismatch")
    out = bytearray(expected)
    for y in range(height):
        row = y * width
        for x in range(width):
            src = (row + x) * 2
            dst = _morton_index(x, y) * 2
            out[dst : dst + 2] = payload[src : src + 2]
    return bytes(out)


def _rgba_from_16(payload: bytes, pixel_format: int) -> bytes:
    out = bytearray((len(payload) // 2) * 4)
    oi = 0
    for i in range(0, len(payload), 2):
        value = payload[i] | (payload[i + 1] << 8)
        if pixel_format == 1:  # RGB565
            r = ((value >> 11) & 0x1F) * 255 // 31
            g = ((value >> 5) & 0x3F) * 255 // 63
            b = (value & 0x1F) * 255 // 31
            a = 255
        elif pixel_format == 2:  # ARGB4444
            a = ((value >> 12) & 0x0F) * 17
            r = ((value >> 8) & 0x0F) * 17
            g = ((value >> 4) & 0x0F) * 17
            b = (value & 0x0F) * 17
        else:  # pragma: no cover - caller guards this
            raise ValueError(f"unsupported 16bpp pixel format: {pixel_format}")
        out[oi : oi + 4] = bytes((r, g, b, a))
        oi += 4
    return bytes(out)


def decode_pvr(data: bytes) -> tuple[Image.Image, PvrHeader]:
    """Decode every PVR variant observed in the target game to RGBA.

    Observed combinations:
      RGB565/ARGB4444 + TWIDDLED or RECTANGLE
      ARGB8888/RGB888 + RECTANGLE
    """

    header = parse_pvr_header(data)
    payload = data[header.payload_offset : header.payload_offset + header.payload_size]
    pixel_count = header.width * header.height

    if header.pixel_format in (1, 2):
        expected = pixel_count * 2
        if len(payload) != expected:
            raise ValueError(
                f"16bpp PVRT payload mismatch: expected={expected} actual={len(payload)}"
            )
        if header.data_format == 1:
            payload = _detwiddle_16(payload, header.width, header.height)
        elif header.data_format != 9:
            raise ValueError(f"unsupported PVR data format: {header.data_format:#x}")
        rgba = _rgba_from_16(payload, header.pixel_format)
    elif header.pixel_format == 6:
        if header.data_format != 9:
            raise ValueError("ARGB8888 is only observed as RECTANGLE in this game")
        expected = pixel_count * 4
        if len(payload) != expected:
            raise ValueError("ARGB8888 payload-size mismatch")
        # PVR stores ARGB8888 as a little-endian 32-bit AARRGGBB word, i.e.
        # byte order B,G,R,A in the file.
        rgba = bytearray(expected)
        for i in range(0, expected, 4):
            b, g, r, a = payload[i : i + 4]
            rgba[i : i + 4] = bytes((r, g, b, a))
        rgba = bytes(rgba)
    elif header.pixel_format == 7:
        if header.data_format != 9:
            raise ValueError("RGB888 is only observed as RECTANGLE in this game")
        expected = pixel_count * 3
        if len(payload) != expected:
            raise ValueError("RGB888 payload-size mismatch")
        # The direct 24-bit extension is stored B,G,R, matching the little-endian
        # direct-colour path used by the 32-bit variant above.
        rgba = bytearray(pixel_count * 4)
        oi = 0
        for i in range(0, expected, 3):
            b, g, r = payload[i : i + 3]
            rgba[oi : oi + 4] = bytes((r, g, b, 255))
            oi += 4
        rgba = bytes(rgba)
    else:
        raise ValueError(
            f"unsupported PVR pixel format {header.pixel_format:#x} "
            f"({PVR_PIXEL_NAMES.get(header.pixel_format, 'unknown')})"
        )

    image = Image.frombytes("RGBA", (header.width, header.height), rgba)
    return image, header


def encode_pvr_like(original: bytes, image: Image.Image) -> bytes:
    """Encode an RGBA edit using the exact dimensions/header of a source PVR."""

    header = parse_pvr_header(original)
    rgba = image.convert("RGBA")
    if rgba.size != (header.width, header.height):
        raise ValueError(
            f"PVR replacement size mismatch: expected={(header.width, header.height)} actual={rgba.size}"
        )

    pixels = rgba.tobytes()
    if header.pixel_format == 1:  # RGB565
        linear = bytearray(header.width * header.height * 2)
        oi = 0
        for i in range(0, len(pixels), 4):
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            r5 = (r * 31 + 127) // 255
            g6 = (g * 63 + 127) // 255
            b5 = (b * 31 + 127) // 255
            value = (r5 << 11) | (g6 << 5) | b5
            linear[oi] = value & 0xFF
            linear[oi + 1] = value >> 8
            oi += 2
        payload = bytes(linear)
    elif header.pixel_format == 2:  # ARGB4444
        linear = bytearray(header.width * header.height * 2)
        oi = 0
        for i in range(0, len(pixels), 4):
            r, g, b, alpha = pixels[i : i + 4]
            value = (
                (((alpha * 15 + 127) // 255) << 12)
                | (((r * 15 + 127) // 255) << 8)
                | (((g * 15 + 127) // 255) << 4)
                | ((b * 15 + 127) // 255)
            )
            linear[oi] = value & 0xFF
            linear[oi + 1] = value >> 8
            oi += 2
        payload = bytes(linear)
    elif header.pixel_format == 6:  # ARGB8888, stored B,G,R,A
        if header.data_format != 9:
            raise ValueError("ARGB8888 replacement requires RECTANGLE storage")
        direct = bytearray(header.width * header.height * 4)
        oi = 0
        for i in range(0, len(pixels), 4):
            r, g, b, alpha = pixels[i : i + 4]
            direct[oi : oi + 4] = bytes((b, g, r, alpha))
            oi += 4
        payload = bytes(direct)
    elif header.pixel_format == 7:  # RGB888, stored B,G,R
        if header.data_format != 9:
            raise ValueError("RGB888 replacement requires RECTANGLE storage")
        direct = bytearray(header.width * header.height * 3)
        oi = 0
        for i in range(0, len(pixels), 4):
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            direct[oi : oi + 3] = bytes((b, g, r))
            oi += 3
        payload = bytes(direct)
    else:
        raise ValueError(f"PVR replacement encoder does not support pixel format {header.pixel_format}")

    if header.data_format == 1:
        payload = _twiddle_16(payload, header.width, header.height)
    elif header.data_format != 9:
        raise ValueError(f"unsupported PVR replacement data format: {header.data_format:#x}")
    if len(payload) != header.payload_size:
        raise ValueError("encoded PVR payload changed size")
    out = bytearray(original)
    out[header.payload_offset : header.payload_offset + header.payload_size] = payload
    return bytes(out)


def color_grade_image_rgb(
    image: Image.Image,
    *,
    brightness: int = 0,
    gamma: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
) -> Image.Image:
    """Apply a restrained RGB color grade while preserving alpha exactly.

    Gamma is applied first to lift or lower midtones without the heavy clipping
    caused by a large fixed RGB offset. Contrast and saturation are then applied
    to RGB only, followed by a small integer brightness trim.
    """

    if gamma <= 0:
        raise ValueError("gamma must be positive")
    if contrast < 0:
        raise ValueError("contrast must be non-negative")
    if saturation < 0:
        raise ValueError("saturation must be non-negative")

    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    if gamma != 1.0:
        gamma_lut = [
            max(0, min(255, round(((value / 255.0) ** gamma) * 255.0)))
            for value in range(256)
        ]
        r, g, b = r.point(gamma_lut), g.point(gamma_lut), b.point(gamma_lut)
    rgb = Image.merge("RGB", (r, g, b))
    if contrast != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
    if saturation != 1.0:
        rgb = ImageEnhance.Color(rgb).enhance(saturation)
    if brightness:
        brightness_lut = [max(0, min(255, value + brightness)) for value in range(256)]
        r, g, b = rgb.split()
        rgb = Image.merge(
            "RGB",
            (r.point(brightness_lut), g.point(brightness_lut), b.point(brightness_lut)),
        )
    r, g, b = rgb.split()
    return Image.merge("RGBA", (r, g, b, a))


def brighten_image_rgb(image: Image.Image, amount: int) -> Image.Image:
    """Add a fixed RGB offset while preserving alpha exactly."""

    return color_grade_image_rgb(image, brightness=amount)


def color_grade_texture_container(
    data: bytes,
    *,
    brightness: int = 0,
    gamma: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
) -> tuple[bytes, int]:
    """Color-grade every PVR texture in a raw PVR or FL/Lzs3 container."""

    unchanged = brightness == 0 and gamma == 1.0 and contrast == 1.0 and saturation == 1.0
    if unchanged:
        return data, 0

    def grade(image: Image.Image) -> Image.Image:
        return color_grade_image_rgb(
            image,
            brightness=brightness,
            gamma=gamma,
            contrast=contrast,
            saturation=saturation,
        )

    if data.startswith((b"PVRT", b"GBIX")):
        image, _ = decode_pvr(data)
        return encode_pvr_like(data, grade(image)), 1
    if not data.startswith((b"FL\0\0", b"Lzs3")):
        return data, 0

    compressed = data.startswith(b"Lzs3")
    raw, chunks = parse_fl(data)
    out = bytearray(raw)
    changed = 0
    for chunk in chunks:
        if not chunk.data.startswith((b"PVRT", b"GBIX")):
            continue
        image, _ = decode_pvr(chunk.data)
        encoded = encode_pvr_like(chunk.data, grade(image))
        if len(encoded) != chunk.size:
            raise ValueError(f"color grade changed PVR size at FL chunk {chunk.index}")
        out[chunk.offset : chunk.offset + chunk.size] = encoded
        changed += 1
    rebuilt = bytes(out)
    if compressed and changed:
        rebuilt = lzs3_compress(rebuilt)
    return rebuilt, changed


def brighten_texture_container(data: bytes, amount: int) -> tuple[bytes, int]:
    """Brighten every PVR texture in a raw PVR or FL/Lzs3 container."""

    return color_grade_texture_container(data, brightness=amount)


def png_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False)
    return out.getvalue()


def iter_pvr_chunks(data: bytes) -> Iterable[tuple[str, bytes]]:
    """Yield PVR-bearing chunks from a raw PVR or FL/Lzs3-wrapped container."""

    if data.startswith((b"PVRT", b"GBIX")):
        yield "pvr00", data
        return
    if data.startswith((b"FL\0\0", b"Lzs3")):
        _, chunks = parse_fl(data)
        n = 0
        for chunk in chunks:
            if chunk.data.startswith((b"PVRT", b"GBIX")):
                yield f"pvr{n:02d}", chunk.data
                n += 1
        return


def encode_five_tile_screen_like(original: bytes, screen: Image.Image) -> bytes:
    """Encode a 640x480 preview back into the game's five 256x256 FL tiles."""

    if original.startswith(b"Lzs3"):
        raise ValueError("five-tile replacement does not support compressed FL containers")
    raw, chunks = parse_fl(original)
    pvr_chunks = [chunk for chunk in chunks if chunk.data.startswith((b"PVRT", b"GBIX"))]
    if len(pvr_chunks) != 5:
        raise ValueError(f"five-tile replacement expected 5 PVR chunks, got {len(pvr_chunks)}")
    target = screen.convert("RGBA")
    if target.size != (640, 480):
        raise ValueError(f"five-tile replacement requires 640x480, got {target.size}")

    originals = [decode_pvr(chunk.data)[0] for chunk in pvr_chunks]
    if any(image.size != (256, 256) for image in originals):
        raise ValueError("five-tile replacement requires five 256x256 textures")
    tiles = [image.copy() for image in originals]
    tiles[0].paste(target.crop((0, 0, 256, 256)), (0, 0))
    tiles[2].paste(target.crop((256, 0, 512, 256)), (0, 0))
    tiles[1].paste(target.crop((0, 256, 256, 480)), (0, 0))
    tiles[3].paste(target.crop((256, 256, 512, 480)), (0, 0))
    tiles[4].paste(target.crop((512, 0, 640, 256)), (0, 0))
    tiles[4].paste(target.crop((512, 256, 640, 480)), (128, 0))

    out = bytearray(raw)
    for chunk, tile in zip(pvr_chunks, tiles, strict=True):
        encoded = encode_pvr_like(chunk.data, tile)
        if len(encoded) != chunk.size:
            raise ValueError(f"five-tile PVR size changed at chunk {chunk.index}")
        out[chunk.offset : chunk.offset + chunk.size] = encoded
    if len(out) != len(original):
        raise AssertionError("five-tile replacement changed container size")
    return bytes(out)
