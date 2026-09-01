# 디지캐럿 판타지 엑설런트 한국어 패치

PlayStation 2용 **『デ・ジ・キャラット ファンタジー エクセレント』 일본판 Premium-ban**의 비공식 한국어 번역 패치입니다.

**삐요코 루트를 제외한 기존 루트는 Windows판 `DigiCarr_Win` 한국어 버전의 번역·그래픽·영상 자산을 PS2판 구조에 포팅하는 방식으로 제작했습니다.** 삐요코 루트와 PS2/Excellent 전용 문장·시스템 문구는 별도로 번역·검수했습니다.

> 이 패치는 비공식 팬 번역입니다. 원작 및 게임 데이터의 권리는 각 권리자에게 있습니다.
>
> 원본 ISO는 제공하지 않습니다. 반드시 본인이 정당하게 소유한 원본에서 직접 준비해 주세요.

### 현재 저장소 소스 상태

현재 Git 소스에는 아래의 **기존 v0.1 Release 이후 수정 사항도 포함**되어 있습니다. 따라서 아래 `v0.1 패치 파일`의 해시/반영 범위는 기존 Release 자체의 기록이며, 현재 소스 스냅샷과 동일하다는 뜻은 아닙니다.

- 이름 결정 확인창 시스템 문자열 추가: 시스템 문자열 `125 / 125`
- 선택지·장면 제목·확인문 등 보조 문자열 `604개` 재삽입
- 개발/테스트 SCX의 일본어 `tX` `349개` 추가 처리
- 현재 로컬 최종 검증본: `digicarr_fantasy_excellent_kr_complete_v29.iso`
- v29 SHA-256: `ce1b2f04f92e81a73d0792031ef66fc5ade2cfabf87284707b3024a403cfcd30`
- v29은 밝기·감마·대비·채도 추가 보정을 사용하지 않은 원본 색상 빌드입니다.

이미지와 영상 파생 자산은 Git에서 제외하므로, 위 v29 바이너리 자체를 이 저장소만으로 재현할 수는 없습니다.

## 1. 패치 대상 원본

아래 **일본판 Premium-ban ISO와 정확히 일치하는 원본**만 지원합니다.

| 항목 | 값 |
| --- | --- |
| 게임 | Di Gi Charat Fantasy Excellent (Japan) (Premium-ban) |
| PS2 실행 파일 | `SLPM_653.95` |
| ISO 크기 | `1,646,854,144 bytes` |
| MD5 | `6f46dd1d05505901fc026128cb6330d8` |
| SHA-1 | `0116852fb3c8973ffd91df3691de758ca64c0963` |
| SHA-256 | `580e033b1833aadb98bec2dd083f24000d09d7201df3473cdbeb8f56a46433e5` |

다른 리전, 다른 리비전, 이미 수정된 ISO에는 적용하지 마세요.

## 2. 패치 적용 방법

일반 사용자는 소스 코드를 빌드할 필요가 없습니다.

1. GitHub **Releases**에서 `digicarr_fantasy_excellent_kr_v0.1.xdelta`를 받습니다.
2. [Delta Patcher](https://github.com/marco-calautti/DeltaPatcher/releases) 등을 실행합니다.
3. `Original file`에 위 해시와 일치하는 일본판 원본 ISO를 지정합니다.
4. `XDelta patch`에 `digicarr_fantasy_excellent_kr_v0.1.xdelta`를 지정합니다.
5. 패치를 적용해 새 ISO를 만듭니다.

원본 ISO에 직접 덮어쓰기보다는 새 출력 파일을 만드는 것을 권장합니다.

### v0.1 패치 파일

| 항목 | 값 |
| --- | --- |
| 파일명 | `digicarr_fantasy_excellent_kr_v0.1.xdelta` |
| 크기 | `108,398,117 bytes` |
| SHA-256 | `0fa14b8247fe6c0fac9466b4834fcc0144cdab3e97c8e90f60be3ce37ffe7602` |

패치 생성 후 깨끗한 원본 ISO에 다시 적용해 최종 ISO와 **byte-for-byte 동일**한 것을 검증했습니다.

### 패치 적용 후 결과 ISO

| 항목 | 값 |
| --- | --- |
| 크기 | `1,646,854,144 bytes` |
| MD5 | `7a7581028b5275ad7039ec8a0249987d` |
| SHA-1 | `ec0ef0398f48ed2b1954ed9ed1985f4eba242c0c` |
| SHA-256 | `2409d226232eaa2f3262ae96a7fe26e9b677abeea19f8a5a8caed59372ee9122` |

## 3. v0.1 번역 범위

최종 검증 기준으로 다음 항목이 반영되어 있습니다.

| 영역 | 반영 상태 |
| --- | ---: |
| 본편 시나리오 | `17,022 / 17,022` |
| PS2 시스템 문자열 | `124 / 124` |
| 크레딧 | `184줄` |
| ETC 이미지 | `11장` |
| BG 이미지 | `9장` |
| 자막/영상 교체 | `13개 SFD` |
| SCRIPT.AFS 엔트리 이동 | `0` |

번역 대상 사용자 표시 문자열 기준으로 일본어 잔존을 전수 감사했습니다.

다만 아래 두 타이틀 로고는 한글 재작성 시 원본 디자인 차이가 커져 **의도적으로 일본어 원본 디자인을 유지**했습니다.

- `title_pt0.pvr`
- `title_pt1.pvr`

또한 `event.scx`, `test.scx`, `test_0.scx`에는 개발자용 테스트 문자열이 물리적으로 남아 있으나 제품 진행에서 사용하는 번역 대상 데이터가 아니므로 제외했습니다.

## 4. 주요 적용 내용

- 삐요코 루트를 제외한 기존 루트: Windows 한국판 번역/그래픽/영상 자산을 PS2판에 포팅
- 삐요코 루트 및 Excellent/PS2 전용 시나리오: 별도 번역·검수
- 설정, 메모리 카드, 세이브/로드, 포맷, 컨트롤러 등 PS2 시스템 메시지 한글화
- 이름 입력 한글 지원
- 24×24 PS2 폰트에 한글 글리프 삽입
- 크레딧 한글화
- ETC/BG 그래픽 한글화
- PS2 영상 13개 자막 반영
- 원본 ISO 크기와 멤버 배치를 유지하는 재현 가능한 빌드/검증

## 5. 번역 품질 및 제보

기존 Windows 한국판 번역을 최대한 활용했으며, Excellent/PS2 전용 신규 문장은 AI 번역을 기반으로 수동 수정과 반복 검증을 진행했습니다.

게임 진행에 지장이 없도록 검증했지만 다음과 같은 부분이 남아 있을 수 있습니다.

- 문맥에 따라 어색한 표현 또는 오역
- 화자에 따른 존댓말/반말 불일치
- 캐릭터 고유 말투 차이
- 미처 발견하지 못한 표시 문제

문제가 발견되면 GitHub Issues에 **발생 화면, 상황, 가능하면 세이브 위치**를 함께 남겨 주세요.

## 6. 저장소 구성

공개 저장소에는 원본 게임 바이너리와 빌드 ISO를 포함하지 않습니다.

- `assets/translation/scenario.json` — 최종 시나리오 번역 데이터
- `assets/translation/scenario_auxiliary.json` — 선택지·장면 제목·확인문 및 보조 문자열 번역 데이터
- `assets/translation/system_strings*.json` — 시스템/UI 번역 데이터
- `assets/translation/credits_worklist.json` — 크레딧 번역 데이터
- `tools/` — 최종 ISO 빌드/검증 및 SFD 처리에 필요한 공개 도구
- `config/inputs.json` — 지원 원본 해시/경로 정보

그래픽 번역에 사용한 PNG 등 **이미지 자산은 Git 저장소에 포함하지 않습니다.** 최종 패치에는 반영되어 있지만 공개 Git에는 번역 데이터와 재현/검증용 코드만 올립니다.

다음 항목은 `.gitignore`로 저장소에서 제외합니다.

- 원본 ISO 및 원본 게임 파일
- `assets/extraction/`의 대용량 추출물
- 빌드된 ISO/PAK/AFS/PVR/SFD 파생 데이터
- PNG/JPG/PSD 등 모든 이미지 자산과 이미지 검토본
- `build/`, `release/`, `.deps/`, 캐시
- AI 번역 중간본, 비교 자료, 내부 작업 메모

`assets/extraction/ps2/SLPM_653.95.japanese_strings_all.json`만 시스템 문자열 원문/오프셋 검증에 필요한 작은 메타데이터 파일이라 예외적으로 저장소에 포함합니다.

## 7. 개발용 직접 빌드

일반 사용자에게는 Release의 XDelta 사용을 권장합니다. 전체 ISO를 직접 재빌드하려면 저장소에 포함되지 않는 원본/파생 자산, **한글화 이미지 자산**, 영상 자산과 한글 폰트가 추가로 필요합니다. Git 저장소만으로는 그래픽/영상까지 포함한 최종 배포 ISO를 byte-for-byte 재현할 수 없습니다.

Python 의존성:

```bash
pip install -r requirements.txt
```

기본 빌더:

```bash
python tools/build_translation_iso.py \
  --output build/digicarr_fantasy_excellent_kr.iso \
  --require-complete-scenario \
  --require-complete-system \
  --require-complete-credits \
  --font /path/to/Pretendard-Regular.otf
```

빌더는 원본 ISO SHA-256, SCX 원문, 포인터, AFS 정렬, 글리프 코드 충돌, 시스템 문자열 슬롯, ETC/BG 재삽입, 최종 ISO readback 등을 검증합니다.

영상 자막용 SFD는 원본 게임 데이터를 포함하는 파생 파일이므로 저장소에 포함하지 않습니다. 개발자가 전체 release ISO를 재현하려면 로컬 작업공간에서 영상 재빌드 단계까지 준비해야 합니다.

## 8. 배포 원칙

- Git 저장소에는 원본/패치된 ISO를 올리지 않습니다.
- GitHub Release에는 `.xdelta` 패치만 배포합니다.
- 패치는 지원 대상 원본의 해시가 정확히 일치할 때만 사용하세요.
- 이 프로젝트는 원작사 및 공식 유통사와 관계없는 비공식 팬 프로젝트입니다.
