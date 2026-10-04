# jev-suneung

2026학년도 수능 국어(홀수형)를 [TypeSafe](https://console.typesafe.ai/) jev API로 풀고 채점합니다.
지문 세트 하나를 `system_one` 호출 한 번으로 보내고, 문항별 선택지 확률까지 받아 기록합니다.

## 결과 (`jev-latest`, 2026-10-04 실행)

| 선택과목 | 원점수 | 등급 | 등급 컷 | 1등급까지 |
|---|---|---|---|---|
| 공통 + 화법과 작문 | 83 / 100 | 2등급 | 83 | 7점 |
| 공통 + 언어와 매체 | 79 / 100 | 2등급 | 78 | 6점 |

| 영역 | 득점 | 정답 | 정답 평균확률 |
|---|---|---|---|
| 독서 | 35 / 38 | 16 / 17 | 0.8647 |
| 문학 | 29 / 38 | 13 / 17 | 0.5918 |
| 화법과 작문 | 19 / 24 | 9 / 11 | 0.7664 |
| 언어와 매체 | 15 / 24 | 7 / 11 | 0.5764 |

틀린 문항: 17, 18, 19, 21, 22 (공통) · 38, 45 (화작) · 36, 37, 39, 41 (언매)

문항별 예측과 ①~⑤ 확률은 [results/graded.csv](results/graded.csv)에 있습니다.

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
  responses.jsonl                     세트별 API 응답(재채점·분석용)
  graded.csv                          문항별 예측, 확신도, 선택지 확률, 득점
  summary.json                        영역별 점수와 정답률
```

## 출처

문제와 정답은 한국교육과정평가원이 공개한 2026학년도 대학수학능력시험 국어 영역(홀수형) 문제지와 정답표를 옮긴 것입니다. 원본 PDF는 저장소에 포함하지 않으며, 아래 게시판에서 받을 수 있습니다.

- [대학수학능력시험 기출문제 (한국교육과정평가원)](https://www.suneung.re.kr/boardCnts/list.do?boardID=1500234&m=0403&s=suneung)

## 참고

`results/`는 실행할 때마다 덮어씁니다. 다른 모델 결과를 따로 남기려면 `--out results/<이름>`을 지정하세요.
