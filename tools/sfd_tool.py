"""Sofdec SFD (MPEG-1 PS + CRI ADX) demuxer / ADX decoder.

Di Gi Charat Fantasy Excellent (PS2) /MOV/*.SFD:
  stream 0xE0 = MPEG-1 video ES
  stream 0xC0 = CRI ADX (type 0x03, 48 kHz stereo) -- NOT MPEG audio.
  ffmpeg/OpenCV mis-detect 0xC0 as MP2 and drop it, which is why earlier
  windows_viewable exports were silent.
"""
import math
import struct
import sys


def demux(path):
    """Return (video_es_bytes, adx_bytes)."""
    d = open(path, 'rb').read()
    vid = bytearray()
    aud = bytearray()
    i = 0
    n = len(d)
    while i < n - 4:
        if d[i:i + 4] == b'\x00\x00\x01\xba':      # MPEG-1 pack header
            i += 12
            continue
        if d[i:i + 3] != b'\x00\x00\x01':          # trailing 0xff padding
            break
        sid = d[i + 3]
        if sid == 0xb9:                            # end of stream
            i += 4
            continue
        ln = int.from_bytes(d[i + 4:i + 6], 'big')
        end = i + 6 + ln
        if sid in (0xbb, 0xbf, 0xbe):              # system / private2 / padding
            i = end
            continue
        p = i + 6
        while d[p] == 0xff:                        # stuffing
            p += 1
        if d[p] & 0xc0 == 0x40:                    # STD buffer scale/size
            p += 2
        t = d[p] & 0xf0
        if t == 0x20:                              # PTS
            p += 5
        elif t == 0x30:                            # PTS + DTS
            p += 10
        elif d[p] == 0x0f:
            p += 1
        if sid == 0xe0:
            vid += d[p:end]
        elif sid == 0xc0:
            aud += d[p:end]
        i = end
    return bytes(vid), bytes(aud)


def adx_info(adx):
    if adx[:2] != b'\x80\x00':
        raise ValueError('not ADX')
    off, enc, bs, bits, ch = struct.unpack_from('>HBBBB', adx, 2)
    rate, total = struct.unpack_from('>II', adx, 8)
    hp, ver = struct.unpack_from('>HB', adx, 16)
    return dict(data_off=off + 4, enc=enc, block=bs, bits=bits,
                channels=ch, rate=rate, samples=total, highpass=hp, version=ver)


def _coeffs(hp, rate):
    sq2 = math.sqrt(2.0)
    z = math.cos(2.0 * math.pi * hp / rate)
    a = sq2 - z
    b = sq2 - 1.0
    c = (a - math.sqrt((a + b) * (a - b))) / b
    return int(c * 2.0 * 4096), int(-(c * c) * 4096)


def adx_decode(adx):
    """Decode standard (type 0x03, 4-bit) ADX -> (info, interleaved int16 list)."""
    info = adx_info(adx)
    if info['enc'] != 0x03 or info['bits'] != 4:
        raise ValueError('unsupported ADX encoding %r' % info)
    c1, c2 = _coeffs(info['highpass'], info['rate'])
    ch = info['channels']
    bs = info['block']
    spb = (bs - 2) * 8 // info['bits']             # 32 samples per block
    body = adx[info['data_off']:]
    nblk = len(body) // bs
    nfrm = nblk // ch
    total = min(info['samples'], nfrm * spb)

    out = [0] * (total * ch)
    for c in range(ch):
        h1 = h2 = 0
        idx = c                                    # output index
        base = c * bs
        written = 0
        for f in range(nfrm):
            o = base + f * ch * bs
            scale = body[o] << 8 | body[o + 1]
            o += 2
            for k in range(spb):
                byte = body[o + (k >> 1)]
                nib = (byte >> 4) if (k & 1) == 0 else (byte & 0x0f)
                if nib >= 8:
                    nib -= 16
                s = nib * scale + ((c1 * h1 + c2 * h2) >> 12)
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                h2 = h1
                h1 = s
                if written < total:
                    out[idx] = s
                    idx += ch
                    written += 1
    return info, out


def write_wav(path, info, samples):
    import array
    a = array.array('h', samples)
    if sys.byteorder == 'big':
        a.byteswap()
    data = a.tobytes()
    ch, rate = info['channels'], info['rate']
    hdr = (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
           struct.pack('<IHHIIHH', 16, 1, ch, rate, rate * ch * 2, ch * 2, 16) +
           b'data' + struct.pack('<I', len(data)))
    with open(path, 'wb') as f:
        f.write(hdr)
        f.write(data)


if __name__ == '__main__':
    src, vout, aout = sys.argv[1], sys.argv[2], sys.argv[3]
    v, a = demux(src)
    open(vout, 'wb').write(v)
    info, s = adx_decode(a)
    write_wav(aout, info, s)
    print(src, info, 'video=%d' % len(v), 'frames=%d' % (len(s) // info['channels']))
