"""Arabic presentation of numeric quantities; leave lexical lookalikes alone."""
from __future__ import annotations

import re

DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
NATIVE = dict(zip(("한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉"), range(1, 10)))
TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50,
        "예순": 60, "일흔": 70, "여든": 80, "아흔": 90}
RANGES = {"한두": "1~2", "두세": "2~3", "서너": "3~4", "너덧": "4~5", "대여섯": "5~6"}
SINO = dict(zip("일이삼사오육칠팔구", range(1, 10)))
JP_DIGITS = dict(zip("一二三四五六七八九", range(1, 10)))
UNITS = ("개월", "시간", "사람", "마리", "번째", "차례", "봉지", "마디", "걸음", "자루", "주일", "미터", "계단",
         "살", "년", "달", "일", "분", "초", "시", "번", "명", "개", "곳", "가지", "잔", "장", "대",
         "배", "층", "줄", "권", "주", "째", "끼", "평", "호", "점")
JP_UNITS = {
    "년": "年", "살": "歳才", "달": "月", "개월": "月", "일": "日", "주": "週", "주일": "週",
    "시": "時", "시간": "時", "분": "分", "초": "秒", "명": "人", "사람": "人", "개": "個つ",
    "번": "回度番", "번째": "回度番", "배": "倍", "층": "階", "장": "枚", "대": "台", "끼": "食",
    "자루": "本", "잔": "杯", "권": "冊", "줄": "行列", "마리": "匹頭", "가지": "種類つ",
    "곳": "所", "마디": "言", "걸음": "歩", "차례": "回度", "봉지": "袋", "째": "番日回",
    "미터": "メ米", "계단": "段", "평": "坪", "호": "号", "점": "点",
}
native_forms = list(NATIVE) + list(TENS) + list(RANGES)
native_forms += [t + n for t in TENS for n in NATIVE]
SINO_FORM = (r"(?=[일이삼사오육칠팔구십백천만])(?:[일이삼사오육칠팔구]?만)?"
             r"(?:[일이삼사오육칠팔구]?천)?(?:[일이삼사오육칠팔구]?백)?"
             r"(?:[일이삼사오육칠팔구]?십)?[일이삼사오육칠팔구]?")
NUMBER = "(?:" + "|".join(sorted(native_forms, key=len, reverse=True)) + "|" + SINO_FORM + ")"
COUNTER = re.compile(r"(?<![가-힣])(" + NUMBER + r")[ \u3000\n]*(" + "|".join(UNITS) + r")")


def sino_value(text: str, digits: dict[str, int]) -> int:
    total = section = value = 0
    for ch in text:
        if ch in digits:
            value = digits[ch]
        elif ch in "十百千십백천":
            power = {"十": 10, "십": 10, "百": 100, "백": 100, "千": 1000, "천": 1000}[ch]
            section += (value or 1) * power
            value = 0
        elif ch in "万만":
            total += (section + value or 1) * 10000
            section = value = 0
        else:
            raise ValueError(text)
    return total + section + value


def numeric_value(text: str) -> str:
    if text in RANGES:
        return RANGES[text]
    if text in NATIVE:
        return str(NATIVE[text])
    for tens, value in TENS.items():
        if text == tens:
            return str(value)
        if text.startswith(tens) and text[len(tens):] in NATIVE:
            return str(value + NATIVE[text[len(tens):]])
    return str(sino_value(text, SINO))


def japanese_quantity(source: str, value: str, unit: str) -> bool:
    """Disambiguate 이곳/이번/사명/백일몽/팔장/한층/한시 using the source."""
    source = source.translate(DIGITS).replace("\n", "")
    counters = JP_UNITS.get(unit, "")
    for match in re.finditer(r"([0-9一二三四五六七八九十百千万]+)[ 　]*(?:ヶ|か|箇)?([" + counters + r"])", source):
        raw = match[1]
        number = int(raw) if raw.isdigit() else sino_value(raw, JP_DIGITS)
        if str(number) == value:
            return True
    return False


def arabic_numbers(text: str, source_ja: str) -> str:
    text = text.translate(DIGITS)
    text = re.sub(r"일[ 　]*분[ 　]*일[ 　]*초", "1분 1초", text)

    def replace(match):
        raw, unit = match[1], match[2]
        value = numeric_value(raw)
        # Native counter forms are unambiguous except 층(한층 더) and 시(한시도).
        native = raw in native_forms
        if (not native or unit in ("층", "시", "일") or raw == "네") and not japanese_quantity(source_ja, value, unit):
            return match[0]
        tail = text[match.end():]
        if match[0].replace(" ", "").replace("\u3000", "") == "일일" and tail.startswith("이"):
            return match[0]  # 일일이 (one by one), even if another clause mentions 一日.
        # Do not replace a prefix inside a lexical word (e.g. 한마디쯤 is a
        # quantity, whereas 한시름 is not a one-o'clock count).
        if tail and "가" <= tail[0] <= "힣" and not re.match(
            r"(?:이|인|일|였|가|은|는|을|를|의|도|만|에|와|과|째|씩|쯤|밖|마저|마다|부터|까지|이라|라고|라|로|으|다|대|간|전|후|동안|내내|보|정도|짜리)", tail
        ):
            return match[0]
        output_unit = "명" if unit == "사람" else unit
        # Keep the line count when a legacy screen wrap splits number/unit.
        return "\n" * match[0].count("\n") + value + output_unit

    text = COUNTER.sub(replace, text)
    # Calendar/counting words which do not use an explicit counter suffix.
    for word, value in {"이틀": "2일", "사흘": "3일", "나흘": "4일", "닷새": "5일", "엿새": "6일",
                        "이레": "7일", "여드레": "8일", "아흐레": "9일", "열흘": "10일", "보름": "15일",
                        "하룻밤": "1박", "반년": "6개월"}.items():
        text = re.sub(r"(?<![가-힣])" + word + r"(?=$|[^가-힣]|[이가은는을를의도만에와과째]|동안|정도|전|후|간)", value, text)
    # A daily frequency is clearer as 1일 3끼. Preserve 하루하루 (each day).
    repeated = []
    def keep_repeat(match):
        repeated.append(match[0])
        return f"\ufff0{len(repeated)-1}\ufff1"
    text = re.sub(r"하루[ 　]*하루", keep_repeat, text)
    text = re.sub(r"(?<![가-힣])하루[ 　]+종일", "1일 내내", text)
    for before, after in {"하루가": "1일이", "하루는": "1일은", "하루를": "1일을", "하루와": "1일과"}.items():
        text = re.sub(r"(?<![가-힣])" + before, after, text)
    text = re.sub(r"(?<![가-힣])하루(?=라)", "1일이", text)
    text = re.sub(r"(?<![가-힣])하루", "1일", text)
    for index, original in enumerate(repeated):
        text = text.replace(f"\ufff0{index}\ufff1", original)
    text = re.sub(r"(?<![가-힣])첫[ 　]*번째", "1번째", text)
    return text
