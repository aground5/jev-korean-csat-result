# jev-suneung

2026학년도 수능 국어(홀수형)를 [TypeSafe](https://console.typesafe.ai/) jev API로 풀고 채점합니다.
지문 세트 하나를 `system_one` 호출 한 번으로 보내고, 문항별 선택지 확률까지 받아 기록합니다.

## 결과 (`jev-latest` → `jev-1.13.0`, 2026-10-04, 10회 반복)

같은 입력이라도 회차마다 확률이 조금씩 달라져서 10회를 돌려 집계했습니다.

| 선택과목 | 원점수 중앙값 | 등급 | 원점수 범위 | 등급 분포 |
|---|---|---|---|---|
| 공통 + 화법과 작문 | 80 / 100 | 3등급 (컷 73, 2등급 컷 83) | 80~83 | 3등급 8회, 2등급 2회 |
| 공통 + 언어와 매체 | 76 / 100 | 3등급 (컷 69, 2등급 컷 78) | 76~79 | 3등급 8회, 2등급 2회 |

| 영역 | 득점 중앙값 | 범위 |
|---|---|---|
| 독서 | 35 / 38 | 35 |
| 문학 | 26 / 38 | 26~29 |
| 화법과 작문 | 19 / 24 | 19 |
| 언어와 매체 | 15 / 24 | 15 |

- 56문항 중 55문항은 10회 내내 같은 답을 냈습니다(항상 정답 44, 항상 오답 11).
- 회차마다 갈린 문항은 34번(3점) 하나입니다. 정답 ③과 오답 ④의 평균 확률이 0.27 대 0.30으로 붙어 있어 10회 중 2회만 맞혔고, 이 문항이 2등급과 3등급을 가릅니다.
- 항상 틀린 문항: 17, 18, 19, 21, 22 (공통) · 38, 45 (화작) · 36, 37, 39, 41 (언매)

### 시간과 비용

| | 값 | 기준 |
|---|---|---|
| 호출당 응답 시간 | 0.284초 (중앙값), 0.217~0.650초 | 170회 호출 |
| 시험 전체 (56문항, 17호출) | 1.46초 (중앙값), 1.37~1.79초 | 동시 4건, 첫 호출 시작부터 마지막 호출 종료까지 |
| 시험 1회 비용 | $0.002004 | 입력 47,716토큰 |
| 문항당 비용 | $0.0000358 | 시험 1회 비용 ÷ 56 |

- jev는 한 호출 안의 문항들을 병렬로 풉니다. 한 호출에 1문항이든 5문항이든 응답 시간 중앙값이 0.26초대로 같아서, 호출 시간을 문항 수로 나누지 않습니다.
- 회차마다 맨 처음 동시에 나가는 4건은 연결을 새로 맺느라 느립니다(중앙값 0.41초, 이후 호출은 0.26초).
- 비용은 입력 토큰 100만 개당 $0.042, 출력 무료를 가정한 추정입니다. 이 단가는 공식 가격표가 아니라 외부 글에서 가져온 값이므로, 다르면 `--input-price`로 바꿔 다시 계산하세요.

집계는 [results/repeat-10/aggregate.json](results/repeat-10/aggregate.json), 회차별 점수는 [runs.csv](results/repeat-10/runs.csv), 문항별 정답 횟수와 예측 분포는 [questions.csv](results/repeat-10/questions.csv)에 있습니다.

## 실행

[uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync
cp .env.example .env   # TYPESAFE_API_KEY 입력
uv run run_jev_suneung.py
```

```bash
uv run run_jev_suneung.py --track 언매        # 공통 + 언어와 매체만
uv run run_jev_suneung.py --only 공통_01-03   # 특정 세트만
uv run run_jev_suneung.py --model <모델명>    # 기본값 jev-latest
uv run run_jev_suneung.py --repeat 10 --out results/repeat-10   # 10회 반복 + 집계
uv run run_jev_suneung.py --list-models       # 사용 가능한 모델 확인
```

## 구성

```
run_jev_suneung.py                    실행 + 채점 스크립트
data/
  2026_suneung_korean_jev.json        세트별 요청(state, questions)
  2026_suneung_korean_answers.json    문항별 정답과 배점
  cutoffs/화작.tsv, 언매.tsv            선택과목별 등급 컷(원점수, 표준점수, 백분위)
results/
  repeat-10/                          10회 반복 결과 (위 표의 근거)
    run-01/ … run-10/                 회차별 responses.jsonl, graded.csv, summary.json
    runs.csv                          회차별 점수, 등급, 시간, 비용
    questions.csv                     문항별 정답 횟수, 예측 분포, 정답 확률 평균·표준편차
    aggregate.json                    점수 통계, 등급 분포, 갈린 문항, 시간·비용 합계
  responses.jsonl, graded.csv, summary.json   첫 단일 실행(83점/79점, 옛 형식)
```

## 출처

문제와 정답은 한국교육과정평가원이 공개한 2026학년도 대학수학능력시험 국어 영역(홀수형) 문제지와 정답표를 옮긴 것입니다. 원본 PDF는 저장소에 포함하지 않으며, 아래 게시판에서 받을 수 있습니다.

- [대학수학능력시험 기출문제 (한국교육과정평가원)](https://www.suneung.re.kr/boardCnts/list.do?boardID=1500234&m=0403&s=suneung)

## 참고

회차별 파일에 담기는 값:

- `responses.jsonl`: 호출별 요청 ID, 실제 응답 모델 버전, HTTP 상태, 재시도 횟수, 요청·응답 크기, 시작 시각, 대기 시간, 응답 시간, 토큰, 응답 원문
- `graded.csv`: 문항별 예측, ①~⑤ 확률, 정답 순위, 1·2순위 확률 차, 엔트로피, 로그손실, 브라이어 점수, 속한 호출의 시간·토큰, 문항당 비용
- `summary.json`: 영역별·배점별 점수, 등급, 확률 보정(ECE), 시간 통계, 토큰, 비용, 실행 환경

`--out`을 주지 않으면 `results/` 바로 아래 파일을 덮어씁니다. 다른 모델이나 다른 실행을 따로 남기려면 `--out results/<이름>`을 지정하세요. 표준점수·백분위 추정은 등급 컷 표의 인접한 두 행을 선형 보간한 값입니다.
