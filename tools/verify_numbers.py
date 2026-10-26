"""Check numeral disambiguation and read back changed dialogue from the ISO."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.deps/build'))
from font_codec import HangulCodeMap, decode_korean_text, normalize_text
from formats import AfsArchive, read_iso_file, sha256_file
from korean_numbers import arabic_numbers
from scx import ScxFile


def verify(iso: Path) -> dict:
    cases = [
        ('열살일걸요', '十歳', '10살일걸요'),
        ('팔년', '八年', '8년'),
        ('여섯시다', '六時', '6시다'),
        ('삼일째가', '三日', '3일째가'),
        ('이층이다', '二階', '2층이다'),
        ('두 사람', '二人', '2명'),
        ('２０００', '２０００', '2000'),
        ('일일이 확인한 삼일', '一日、三日', '일일이 확인한 3일'),
        ('이번에도 서둘러！ 넷！ 한층 더', '今回も急いで！ はい！ もっと',
         '이번에도 서둘러！ 넷！ 한층 더'),
        ('하루 하루를 소중히', '毎日を大切に', '하루 하루를 소중히'),
        ('보름달', '満月', '보름달'),
    ]
    for before, ja, expected in cases:
        assert arabic_numbers(before, ja) == expected, (before, expected)
    path = 'assets/translation/scenario.json'
    old = json.loads(subprocess.check_output(['git', 'show', '94e3b6a7d8f89953aec49774cf9c6e46a52453fe:' + path], cwd=ROOT))
    current = json.loads((ROOT / path).read_text(encoding='utf-8'))
    previous = {r['key']: r for r in old['records']}
    changed = {}
    for row in current['records']:
        prior = previous[row['key']]
        assert {k:v for k,v in row.items() if k not in ('target_ko', 'suggested_ko')} == {
            k:v for k,v in prior.items() if k not in ('target_ko', 'suggested_ko')}, row['key']
        if row.get('target_ko') != prior.get('target_ko'):
            changed[row['key']] = row['target_ko']
        assert not re.search('[０-９]', row.get('target_ko') or ''), row['key']
    report = json.loads((ROOT / 'build/reuse_build_report.json').read_text(encoding='utf-8'))
    assert sha256_file(iso) == report['output_iso']['sha256']
    code_map = HangulCodeMap({r['char']: int(r['sjis_code'],16) for r in report['hangul_code_map']})
    afs = AfsArchive(read_iso_file(iso, '/SCRIPT.AFS;1'))
    verified = set()
    for entry in afs.entries:
        for command in ScxFile.parse(afs.read(entry)).commands:
            if command.tag != b'tX':
                continue
            key = f'SCRIPT.AFS/{entry.name}/tX/{command.text_id}'
            if key in changed:
                assert decode_korean_text(command.text, code_map) == normalize_text(changed[key]), key
                verified.add(key)
    assert verified == set(changed)
    return {'ok': True, 'iso_sha256': report['output_iso']['sha256'],
            'numeral_regressions': len(cases), 'changed_dialogue_readback': len(verified),
            'source_metadata_preserved': True, 'fullwidth_digits_remaining_in_dialogue': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('iso', type=Path)
    print(json.dumps(verify(parser.parse_args().iso), ensure_ascii=False, indent=2))
