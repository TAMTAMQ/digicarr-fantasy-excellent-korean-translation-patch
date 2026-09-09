디지캐럿 판타지 엑설런트 한국어 패치 v0.2

대상:
Di Gi Charat Fantasy Excellent (Japan) (Premium-ban)
PS2 / SLPM_653.95

원본 ISO:
크기    1,646,854,144 bytes
MD5     6f46dd1d05505901fc026128cb6330d8
SHA-1   0116852fb3c8973ffd91df3691de758ca64c0963
SHA-256 580e033b1833aadb98bec2dd083f24000d09d7201df3473cdbeb8f56a46433e5

패치:
digicarr_fantasy_excellent_kr_v0.2.xdelta
크기    108,418,072 bytes
SHA-256 b0c501ef534fdef00dddc4604cd8f44dea9f5ca6bd3026ed4bc10a5c2942d6d6

적용:
1. Releases에서 .xdelta 파일을 받습니다.
2. Delta Patcher에서 Original file에 위 해시와 일치하는 원본 ISO를 선택합니다.
3. XDelta patch에 digicarr_fantasy_excellent_kr_v0.2.xdelta를 선택합니다.
4. Apply patch로 새 ISO를 만듭니다.

패치 적용 후 ISO:
크기    1,646,854,144 bytes
MD5     e74dad19c5af8937b8d3715bc0959a94
SHA-1   1431d7b97806e3a5b453ce4a1ede45d5ad109925
SHA-256 dd3210001dfbe9f76d1d60000bc1006e64941090554e878a0b94616e221daf5c

v0.2 주요 수정:
- SCRIPT.AFS 장면 전환 프리징 수정
- SCRIPT.AFS 120개를 원본과 동일한 0x800 순차 배치 규칙으로 재패킹
- 메인 TOC와 filename-directory 보조 TOC 동기화
- 전체 ld 165개, SCX 내부 포인터 74,025개, 제어 필드 189,367개 전수검사
- ETC/BG/EVENT/FACE.PAK의 entry name/offset/size 및 0x800 배치 규칙 검증 강화
- 삐요코 루트 piyo_00~10, 중복 SCX, 저장 분기, 영상/엔딩/리소스 참조 전수검사

제작 방식:
- 삐요코 루트를 제외한 기존 루트는 Windows 한국판 번역/그래픽/영상 자산을 PS2판에 포팅
- 삐요코 루트와 PS2/Excellent 전용 문장·시스템 문구는 별도 번역·검수

반영 범위:
- 시나리오(개발/테스트 SCX 포함) 17,371 / 17,371
- 시스템 문자열 125 / 125
- 선택지·장면 제목·확인문 등 보조 문자열 604 / 604
- 크레딧 184줄
- ETC 이미지 11장
- BG 이미지 9장
- 자막/영상 13개 SFD

주의:
- 다른 버전/리전/이미 수정된 ISO에는 적용하지 마세요.
- title_pt0.pvr / title_pt1.pvr 타이틀 로고는 원본 디자인을 의도적으로 유지했습니다.
- 원본 또는 패치된 ISO는 배포하지 않습니다.
- 비공식 팬 번역이며 원작 및 게임 데이터의 권리는 각 권리자에게 있습니다.
