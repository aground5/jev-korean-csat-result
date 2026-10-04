"""2026학년도 수능 국어(홀수형)를 TypeSafe jev API로 풀고 채점합니다.

준비:
    uv sync
    cp .env.example .env               # TYPESAFE_API_KEY=... (https://console.typesafe.ai/ 에서 발급)

실행 예:
    uv run run_jev_suneung.py                         # 전체 17세트(56문항) 실행 후 채점
    uv run run_jev_suneung.py --track 언매            # 공통 + 언어와 매체만
    uv run run_jev_suneung.py --only 공통_01-03       # 특정 세트만
    uv run run_jev_suneung.py --list-models           # 사용 가능한 모델 확인

입력 파일(data/ 폴더):
    2026_suneung_korean_jev.json      세트별 {"id", "meta", "request": {"state", "questions"}}
    2026_suneung_korean_answers.json  세트별 {"q1": {"정답": "③", "배점": 2}, ...}

출력(--out 폴더, 기본 results/):
    responses.jsonl  세트별 API 원응답(재채점·분석용)
    graded.csv       문항별 예측, 확신도, ①~⑤ 확률, 정답, 득점
    summary.json     영역별 점수와 정답률
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeAPIError, TypeSafeError

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # TYPESAFE_API_KEY를 스크립트 폴더의 .env에서 읽습니다.
OPTIONS = ["①", "②", "③", "④", "⑤"]
SECTION_TRACK = {"독서": "공통", "문학": "공통", "화법과 작문": "화작", "언어와 매체": "언매"}


def load_sets(path: Path, track: str, only: list[str] | None) -> list[dict]:
    sets = json.loads(path.read_text(encoding="utf-8"))
    selected = []
    for s in sets:
        t = SECTION_TRACK[s["meta"]["영역"]]
        if track != "all" and t not in ("공통", track):
            continue
        if only and s["id"] not in only:
            continue
        selected.append(s)
    return selected


async def run_set(client: AsyncTypeSafeClient, s: dict, model: str, sem: asyncio.Semaphore) -> dict:
    """세트 하나(지문 + 딸린 문항들)를 한 번의 system_one 호출로 보냅니다."""
    req = s["request"]
    async with sem:
        started = time.perf_counter()
        try:
            res = await client.system_one(
                # 단독 문항(언매 37~39)은 지문이 없으므로 빈 문자열을 보냅니다.
                state=req.get("state", ""),
                # 질문은 JSON 그대로(dict) 넘깁니다. type/instructions/criteria 구조가 SDK의 ChoiceModel과 같습니다.
                questions=req["questions"],
                model=model,
            )
        except TypeSafeAPIError as e:
            print(f"  ✗ {s['id']}: API 오류 status={e.status} request_id={e.request_id} {e}", file=sys.stderr)
            return {"id": s["id"], "error": f"{type(e).__name__}: {e}"}
        except TypeSafeError as e:
            print(f"  ✗ {s['id']}: {type(e).__name__}: {e}", file=sys.stderr)
            return {"id": s["id"], "error": f"{type(e).__name__}: {e}"}
        elapsed = time.perf_counter() - started

    answers = {
        name: {"choice": a.choice, "confidence": a.confidence, "probabilities": dict(a.probabilities)}
        for name, a in res.choices.items()
    }
    print(f"  ✓ {s['id']}  {len(answers)}문항  {elapsed:.1f}s  "
          f"in={res.usage.input_tokens} out={res.usage.output_tokens}")
    return {
        "id": s["id"],
        "model": res.model,
        "elapsed_s": round(elapsed, 2),
        "usage": {"input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens},
        "answers": answers,
    }


def grade(sets: list[dict], results: list[dict], key: dict) -> tuple[list[dict], dict]:
    by_id = {r["id"]: r for r in results}
    rows = []
    for s in sets:
        r = by_id[s["id"]]
        track = SECTION_TRACK[s["meta"]["영역"]]
        for q in s["request"]["questions"]:
            gold = key[s["id"]][q]
            a = (r.get("answers") or {}).get(q)
            pred = a["choice"] if a else None
            probs = a["probabilities"] if a else {}
            correct = pred == gold["정답"]
            rows.append({
                "set": s["id"], "track": track, "영역": s["meta"]["영역"], "문항": int(q[1:]),
                "예측": pred or "(오류)", "정답": gold["정답"], "정오": "O" if correct else "X",
                "배점": gold["배점"], "득점": gold["배점"] if correct else 0,
                "확신도": round(a["confidence"], 4) if a else None,
                "정답_확률": round(probs.get(gold["정답"], 0.0), 4) if a else None,
                **{o: round(probs.get(o, 0.0), 4) for o in OPTIONS},
            })

    def agg(filter_fn) -> dict:
        rs = [x for x in rows if filter_fn(x)]
        if not rs:
            return {}
        ok = [x for x in rs if x["정오"] == "O"]
        p_gold = [x["정답_확률"] for x in rs if x["정답_확률"] is not None]
        return {
            "문항수": len(rs), "정답수": len(ok), "정답률": round(len(ok) / len(rs), 4),
            "득점": sum(x["득점"] for x in rs), "만점": sum(x["배점"] for x in rs),
            "정답_평균확률": round(sum(p_gold) / len(p_gold), 4) if p_gold else None,
        }

    summary = {
        "영역별": {
            "공통(독서·문학)": agg(lambda x: x["track"] == "공통"),
            "독서": agg(lambda x: x["영역"] == "독서"),
            "문학": agg(lambda x: x["영역"] == "문학"),
            "화법과 작문": agg(lambda x: x["track"] == "화작"),
            "언어와 매체": agg(lambda x: x["track"] == "언매"),
        },
        "100점 환산": {
            "공통 + 화법과 작문": agg(lambda x: x["track"] in ("공통", "화작")),
            "공통 + 언어와 매체": agg(lambda x: x["track"] in ("공통", "언매")),
        },
        "오류 세트": [r["id"] for r in results if "error" in r],
        "토큰": {
            "input_tokens": sum((r.get("usage") or {}).get("input_tokens") or 0 for r in results),
            "output_tokens": sum((r.get("usage") or {}).get("output_tokens") or 0 for r in results),
        },
    }
    return rows, summary


async def main() -> None:
    ap = argparse.ArgumentParser(description="수능 국어를 jev로 풀고 채점합니다.")
    ap.add_argument("--input", type=Path, default=HERE / "data" / "2026_suneung_korean_jev.json")
    ap.add_argument("--answers", type=Path, default=HERE / "data" / "2026_suneung_korean_answers.json")
    ap.add_argument("--model", default="jev-latest", help="모델 이름 (기본: jev-latest)")
    ap.add_argument("--track", choices=["all", "화작", "언매"], default="all",
                    help="all=전체, 화작=공통+화법과 작문, 언매=공통+언어와 매체")
    ap.add_argument("--only", nargs="*", help="실행할 세트 id (예: 공통_01-03 언매_37)")
    ap.add_argument("--concurrency", type=int, default=4, help="동시 요청 수")
    ap.add_argument("--out", type=Path, default=HERE / "results")
    ap.add_argument("--list-models", action="store_true", help="사용 가능한 모델만 출력하고 종료")
    args = ap.parse_args()

    async with AsyncTypeSafeClient(retry=RetryPolicy(max_retries=3), timeout=120.0) as client:
        if args.list_models:
            for m in (await client.models.list()).models:
                print(f"{m.name:24s} {m.release_date}  {m.description}")
            return

        sets = load_sets(args.input, args.track, args.only)
        if not sets:
            sys.exit("실행할 세트가 없습니다. --track / --only 값을 확인하세요.")
        n_q = sum(len(s["request"]["questions"]) for s in sets)
        print(f"모델 {args.model} · {len(sets)}세트 {n_q}문항 · 동시 {args.concurrency}건")

        sem = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*(run_set(client, s, args.model, sem) for s in sets))

    key = json.loads(args.answers.read_text(encoding="utf-8"))
    rows, summary = grade(sets, results, key)
    summary = {"model": args.model, **summary}

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "responses.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (args.out / "graded.csv").open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글이 깨지지 않도록 BOM 포함
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n── 채점 결과 ──")
    for name, v in {**summary["영역별"], **summary["100점 환산"]}.items():
        if v:
            print(f"{name:16s} {v['득점']:>3}/{v['만점']:<3}점  정답 {v['정답수']}/{v['문항수']}  "
                  f"정답 평균확률 {v['정답_평균확률']}")
    wrong = [f"{x['set']} {x['문항']}번(예측 {x['예측']}, 정답 {x['정답']})" for x in rows if x["정오"] == "X"]
    if wrong:
        print("\n틀린 문항:\n  " + "\n  ".join(wrong))
    if summary["오류 세트"]:
        print(f"\n오류로 채점에서 0점 처리된 세트: {', '.join(summary['오류 세트'])}")
    print(f"\n저장: {args.out}/ (responses.jsonl, graded.csv, summary.json)")


if __name__ == "__main__":
    asyncio.run(main())
