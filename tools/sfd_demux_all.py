"""Demux every /MOV/*.SFD into MPEG-1 video ES + decoded PCM WAV."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sfd_tool

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                    'assets', 'extraction', 'ps2', 'MOV')
RAW = os.path.join(ROOT, 'raw')
OUT = os.path.join(ROOT, 'demux')
os.makedirs(OUT, exist_ok=True)

records = []
names = sorted(f for f in os.listdir(RAW) if f.upper().endswith('.SFD'))
for name in names:
    stem = os.path.splitext(name)[0]
    t0 = time.time()
    video, adx = sfd_tool.demux(os.path.join(RAW, name))
    vpath = os.path.join(OUT, stem + '.m1v')
    apath = os.path.join(OUT, stem + '.wav')
    with open(vpath, 'wb') as f:
        f.write(video)
    info, pcm = sfd_tool.adx_decode(adx)
    sfd_tool.write_wav(apath, info, pcm)
    rec = dict(sfd=name, video_es=os.path.basename(vpath), video_bytes=len(video),
               wav=os.path.basename(apath), adx_bytes=len(adx),
               channels=info['channels'], rate=info['rate'],
               pcm_frames=len(pcm) // info['channels'],
               duration_sec=round(len(pcm) / info['channels'] / info['rate'], 3),
               adx=info, seconds=round(time.time() - t0, 1))
    records.append(rec)
    print(json.dumps(rec, ensure_ascii=False), flush=True)

with open(os.path.join(OUT, 'demux_manifest.json'), 'w', encoding='utf-8') as f:
    json.dump({'format': 'digicarr-fe-ps2-sfd-demux-v1',
               'note': 'SFD audio is CRI ADX on stream 0xC0, not MPEG audio.',
               'records': records}, f, ensure_ascii=False, indent=2)
print('done', len(records))
