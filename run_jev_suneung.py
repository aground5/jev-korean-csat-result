"""2026학년도 수능 국어(홀수형)를 TypeSafe jev API로 풀고 채점합니다.

준비:
    uv sync
    cp .env.example .env               # TYPESAFE_API_KEY=... (https://console.typesafe.ai/ 에서 발급)

실행 예:
    uv run run_jev_suneung.py                         # 전체 17세트(56문항) 실행 후 채점
    uv run run_jev_suneung.py --track 언매            # 공통 + 언어와 매체만
    uv run run_jev_suneung.py --only 공통_01-03       # 특정 세트만
    uv run run_jev_suneung.py --repeat 10 --out results/repeat-10   # 10회 반복 + 집계
    uv run run_jev_suneung.py --list-models           # 사용 가능한 모델 확인

입력 파일(data/ 폴더):
    2026_suneung_korean_jev.json      세트별 {"id", "meta", "request": {"state", "questions"}}
    2026_suneung_korean_answers.json  세트별 {"q1": {"정답": "③", "배점": 2}, ...}
    cutoffs/화작.tsv, 언매.tsv          선택과목별 등급 컷(원점수, 표준점수, 백분위)

출력(--out 폴더, 기본 results/):
    responses.jsonl  세트(호출)별 원응답, 요청 ID, HTTP 메타데이터, 시간, 토큰
    graded.csv       문항별 예측, ①~⑤ 확률, 정답 순위, 보정 지표, 호출 시간·토큰·비용
    summary.json     영역별 점수, 등급, 보정, 시간, 토큰, 비용, 실행 환경

--repeat N(2 이상)이면 --out 아래 run-01/ … run-NN/ 에 위 세 파일을 각각 쓰고, 집계를 추가합니다:
    runs.csv         회차별 점수, 등급, 시간, 비용
    questions.csv    문항별 정답 횟수, 회차별 예측 분포, 정답 확률의 평균·표준편차
    aggregate.json   점수 통계, 등급 분포, 예측이 흔들린 문항, 시간·비용 합계
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import platform
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeAPIError, TypeSafeError

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # TYPESAFE_API_KEY를 스크립트 폴더의 .env에서 읽습니다.
OPTIONS = ["①", "②", "③", "④", "⑤"]
SECTION_TRACK = {"독서": "공통", "문학": "공통", "화법과 작문": "화작", "언어와 매체": "언매"}
TRACK_LABEL = {"화작": "공통 + 화법과 작문", "언매": "공통 + 언어와 매체"}
SAVED_HEADERS = ["date", "server", "cf-ray", "cf-cache-status", "content-type", "content-encoding"]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stats(xs: list[float], nd: int = 3) -> dict:
    """평균·중앙값·p90·최소·최대·표준편차."""
    if not xs:
        return {}
    s = sorted(xs)
    return {
        "n": len(s), "평균": round(statistics.fmean(s), nd), "중앙값": round(statistics.median(s), nd),
        "p90": round(s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)], nd),
        "최소": round(s[0], nd), "최대": round(s[-1], nd),
        "표준편차": round(statistics.stdev(s), nd) if len(s) > 1 else 0.0,
    }


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


def load_cutoffs(folder: Path) -> dict[str, list[dict]]:
    """선택과목별 등급 컷. 0등급 행은 만점 기준입니다."""
    out = {}
    for track in TRACK_LABEL:
        path = folder / f"{track}.tsv"
        if not path.exists():
            continue
        lines = [[c.strip() for c in ln.split("\t")] for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out[track] = [
            {"등급": int(r[0].removesuffix("등급")), "원점수": int(r[1]), "표준점수": int(r[2]), "백분위": int(r[3])}
            for r in lines[1:]
        ]
    return out


def grade_of(score: int, cuts: list[dict]) -> dict:
    """원점수 → 등급. 표준점수·백분위는 인접한 두 컷 사이를 선형 보간한 추정치입니다."""
    rows = sorted(cuts, key=lambda r: -r["원점수"])  # 만점(0등급) → 8등급 컷
    real = [r for r in rows if r["등급"] >= 1]
    g = next((r["등급"] for r in real if score >= r["원점수"]), 9)
    res = {"등급": g}
    cut = next((r for r in real if r["등급"] == g), None)
    if cut:
        res["등급컷"] = cut["원점수"]
        res["등급컷_대비"] = score - cut["원점수"]
    upper = next((r for r in real if r["등급"] == g - 1), None)
    if upper:
        res["윗등급까지"] = upper["원점수"] - score
    for lo, hi in zip(rows[1:], rows):
        if lo["원점수"] <= score <= hi["원점수"]:
            f = (score - lo["원점수"]) / (hi["원점수"] - lo["원점수"])
            res["표준점수_추정"] = round(lo["표준점수"] + f * (hi["표준점수"] - lo["표준점수"]), 1)
            res["백분위_추정"] = round(lo["백분위"] + f * (hi["백분위"] - lo["백분위"]), 1)
            break
    return res


async def run_set(client: AsyncTypeSafeClient, s: dict, model: str, sem: asyncio.Semaphore, t0: float) -> dict:
    """세트 하나(지문 + 딸린 문항들)를 한 번의 system_one 호출로 보냅니다."""
    req = s["request"]
    rec: dict = {"id": s["id"], "n_questions": len(req["questions"]), "requested_model": model}
    queued = time.perf_counter()
    async with sem:
        started = time.perf_counter()
        rec["queue_wait_s"] = round(started - queued, 4)   # 동시 요청 한도 때문에 기다린 시간
        rec["start_offset_s"] = round(started - t0, 4)     # 이 회차 시작부터 호출 시작까지
        rec["started_at"] = now_utc()
        try:
            res = await client.system_one(
                # 단독 문항(언매 37~39)은 지문이 없으므로 빈 문자열을 보냅니다.
                state=req.get("state", ""),
                # 질문은 JSON 그대로(dict) 넘깁니다. type/instructions/criteria 구조가 SDK의 ChoiceModel과 같습니다.
                questions=req["questions"],
                model=model,
            )
        except TypeSafeError as e:
            rec["elapsed_s"] = round(time.perf_counter() - started, 4)
            rec["error"] = f"{type(e).__name__}: {e}"
            if isinstance(e, TypeSafeAPIError):
                rec["http_status"], rec["request_id"] = e.status, e.request_id
            print(f"  ✗ {s['id']}: {rec['error']}", file=sys.stderr)
            return rec
        rec["elapsed_s"] = round(time.perf_counter() - started, 4)

    raw = res.raw_http_response
    rec.update({
        "model": res.model,  # 별칭(jev-latest)이 실제로 가리킨 버전
        "request_id": raw.headers.get("x-typesafe-request-id"),
        "http_status": raw.status_code,
        "http_version": raw.http_version,
        "http_elapsed_s": round(raw.elapsed.total_seconds(), 4),  # httpx가 잰 요청 전송~응답 수신
        "request_bytes": len(raw.request.content),
        "response_bytes": len(raw.content),
        "retries": int(next((v for k, v in raw.request.headers.items() if "retry" in k.lower()), 0)),
        "headers": {h: raw.headers[h] for h in SAVED_HEADERS if h in raw.headers},
        "usage": {"input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens},
        "answers": {
            name: {"choice": a.choice, "confidence": a.confidence, "probabilities": dict(a.probabilities)}
            for name, a in res.choices.items()
        },
        "raw_body": json.loads(raw.content),
    })
    print(f"  ✓ {s['id']}  {len(rec['answers'])}문항  {rec['elapsed_s']:.2f}s  "
          f"in={res.usage.input_tokens} out={res.usage.output_tokens}")
    return rec


def question_row(s: dict, q: str, r: dict, gold: dict, input_price: float) -> dict:
    a = (r.get("answers") or {}).get(q)
    n_q = len(s["request"]["questions"])
    usage = r.get("usage") or {}
    row = {
        "set": s["id"], "track": SECTION_TRACK[s["meta"]["영역"]], "영역": s["meta"]["영역"], "문항": int(q[1:]),
        "예측": a["choice"] if a else "(오류)", "정답": gold["정답"],
        "정오": "O" if a and a["choice"] == gold["정답"] else "X",
        "배점": gold["배점"], "득점": gold["배점"] if a and a["choice"] == gold["정답"] else 0,
    }
    if a:
        p = {o: a["probabilities"].get(o, 0.0) for o in OPTIONS}
        ranked = sorted(OPTIONS, key=lambda o: -p[o])
        p_gold = p[gold["정답"]]
        row.update({
            "확신도": round(a["confidence"], 4),
            "정답_확률": round(p_gold, 4),
            "정답_순위": ranked.index(gold["정답"]) + 1,
            "1순위_확률": round(p[ranked[0]], 4),
            "2순위": ranked[1], "2순위_확률": round(p[ranked[1]], 4),
            "1·2순위_차": round(p[ranked[0]] - p[ranked[1]], 4),
            "엔트로피_bit": abs(round(-sum(v * math.log2(v) for v in p.values() if v > 0), 4)),
            "로그손실": abs(round(-math.log(max(p_gold, 0.005)), 4)),  # 확률이 0.01 단위라 0은 0.005로 받칩니다.
            "브라이어": round(sum((p[o] - (o == gold["정답"])) ** 2 for o in OPTIONS), 4),
            "확률합": round(sum(p.values()), 4),
            **{o: round(p[o], 4) for o in OPTIONS},
        })
    else:
        row.update(dict.fromkeys(
            ["확신도", "정답_확률", "정답_순위", "1순위_확률", "2순위", "2순위_확률", "1·2순위_차",
             "엔트로피_bit", "로그손실", "브라이어", "확률합", *OPTIONS]))
    # jev는 한 호출 안의 문항들을 병렬로 풀기 때문에 호출 시간을 문항 수로 나누지 않습니다.
    # 문항 하나의 응답 시간은 그 문항이 속한 호출의 시간입니다. 비용은 토큰 과금이라 문항 수로 나눕니다.
    row.update({
        "호출_문항수": n_q,
        "호출_시간_s": r.get("elapsed_s"),
        "호출_입력토큰": usage.get("input_tokens"),
        "호출_출력토큰": usage.get("output_tokens"),
        "문항당_비용_USD": round((usage.get("input_tokens") or 0) / 1e6 * input_price / n_q, 8),
        "request_id": r.get("request_id"),
        "오류": r.get("error", ""),
    })
    return row


def calibration(rows: list[dict]) -> dict:
    """1순위 확률을 0.2 단위 구간으로 묶어 실제 정답률과 비교합니다."""
    rs = [x for x in rows if x["1순위_확률"] is not None]
    if not rs:
        return {}
    bins, ece = [], 0.0
    for i in range(5):
        lo, hi = i * 0.2, (i + 1) * 0.2
        b = [x for x in rs if lo <= x["1순위_확률"] < hi or (i == 4 and x["1순위_확률"] == 1.0)]
        if not b:
            continue
        conf = statistics.fmean(x["1순위_확률"] for x in b)
        acc = sum(x["정오"] == "O" for x in b) / len(b)
        ece += len(b) / len(rs) * abs(conf - acc)
        bins.append({"구간": f"{lo:.1f}~{hi:.1f}", "문항수": len(b), "평균_1순위_확률": round(conf, 4), "정답률": round(acc, 4)})
    return {"ECE": round(ece, 4), "구간별": bins}


def grade(sets: list[dict], results: list[dict], key: dict, input_price: float, cutoffs: dict) -> tuple[list[dict], dict]:
    by_id = {r["id"]: r for r in results}
    rows = [question_row(s, q, by_id[s["id"]], key[s["id"]][q], input_price)
            for s in sets for q in s["request"]["questions"]]

    def agg(filter_fn) -> dict:
        rs = [x for x in rows if filter_fn(x)]
        if not rs:
            return {}
        ok = [x for x in rs if x["정오"] == "O"]
        ans = [x for x in rs if x["정답_확률"] is not None]

        def mean(col: str):
            return round(statistics.fmean(x[col] for x in ans), 4) if ans else None

        return {
            "문항수": len(rs), "정답수": len(ok), "정답률": round(len(ok) / len(rs), 4),
            "득점": sum(x["득점"] for x in rs), "만점": sum(x["배점"] for x in rs),
            "2순위_이내_정답수": sum(x["정답_순위"] <= 2 for x in ans),
            "정답_평균확률": mean("정답_확률"), "정답_평균순위": mean("정답_순위"),
            "평균_확신도": mean("확신도"), "평균_1순위_확률": mean("1순위_확률"),
            "평균_엔트로피_bit": mean("엔트로피_bit"), "평균_로그손실": mean("로그손실"), "평균_브라이어": mean("브라이어"),
        }

    summary: dict = {
        "영역별": {
            "공통(독서·문학)": agg(lambda x: x["track"] == "공통"),
            "독서": agg(lambda x: x["영역"] == "독서"),
            "문학": agg(lambda x: x["영역"] == "문학"),
            "화법과 작문": agg(lambda x: x["track"] == "화작"),
            "언어와 매체": agg(lambda x: x["track"] == "언매"),
        },
        "100점 환산": {TRACK_LABEL[t]: agg(lambda x, t=t: x["track"] in ("공통", t)) for t in TRACK_LABEL},
        "배점별": {f"{p}점": agg(lambda x, p=p: x["배점"] == p) for p in (2, 3)},
    }
    # 등급은 100점 만점이 다 채워졌고 오류 없이 풀린 선택과목에만 매깁니다.
    summary["등급"] = {}
    for t, label in TRACK_LABEL.items():
        v = summary["100점 환산"][label]
        tr = [x for x in rows if x["track"] in ("공통", t)]
        if t in cutoffs and v and v["만점"] == 100 and not any(x["오류"] for x in tr):
            summary["등급"][label] = {"원점수": v["득점"], **grade_of(v["득점"], cutoffs[t])}
    summary["보정"] = calibration(rows)
    summary["틀린 문항"] = [
        {"문항": x["문항"], "set": x["set"], "예측": x["예측"], "정답": x["정답"], "배점": x["배점"],
         "정답_확률": x["정답_확률"], "정답_순위": x["정답_순위"]}
        for x in rows if x["정오"] == "X"
    ]
    summary["오류 세트"] = [r["id"] for r in results if "error" in r]

    done = [r for r in results if "error" not in r]
    tok_in = sum(r["usage"]["input_tokens"] or 0 for r in done)
    tok_out = sum(r["usage"]["output_tokens"] or 0 for r in done)
    n_q = sum(len(r["answers"]) for r in done)
    summary["호출"] = {
        "호출수": len(results), "성공": len(done), "재시도_합계": sum(r.get("retries", 0) for r in done),
        "응답_모델": sorted({r["model"] for r in done}),
        "요청_bytes": sum(r["request_bytes"] for r in done), "응답_bytes": sum(r["response_bytes"] for r in done),
    }
    summary["토큰"] = {
        "input_tokens": tok_in, "output_tokens": tok_out,
        "호출당_입력": stats([r["usage"]["input_tokens"] or 0 for r in done], 1),
        "문항당_평균_입력": round(tok_in / n_q, 1) if n_q else None,
    }
    summary["시간"] = {
        "호출_시간_s": stats([r["elapsed_s"] for r in done]),          # 문항 하나의 응답 시간 = 그 호출의 시간
        "http_시간_s": stats([r["http_elapsed_s"] for r in done]),
        "대기_시간_s": stats([r["queue_wait_s"] for r in done]),
        "호출_시간_합계_s": round(sum(r["elapsed_s"] for r in done), 3),
    }
    cost = tok_in / 1e6 * input_price  # 출력 토큰은 무료
    summary["비용_USD"] = {
        "입력_단가_per_1M": input_price, "총액": round(cost, 6),
        "호출당": round(cost / len(done), 8) if done else None,
        "문항당": round(cost / n_q, 8) if n_q else None,
    }
    return rows, summary


async def run_once(args, sets: list[dict], key: dict, cutoffs: dict, out: Path, meta: dict) -> tuple[list[dict], dict]:
    """전체 세트를 한 번 풀고 out 폴더에 저장합니다. 회차마다 새 연결로 시작합니다."""
    started_at = now_utc()
    async with AsyncTypeSafeClient(retry=RetryPolicy(max_retries=3), timeout=120.0) as client:
        sem = asyncio.Semaphore(args.concurrency)
        t0 = time.perf_counter()
        results = await asyncio.gather(*(run_set(client, s, args.model, sem, t0) for s in sets))
        wall_s = time.perf_counter() - t0

    rows, summary = grade(sets, results, key, args.input_price, cutoffs)
    n_q = len(rows)
    summary["시간"].update({
        "전체_실행_s": round(wall_s, 3),  # 동시 요청을 포함한 실제 경과 시간
        "초당_문항": round(n_q / wall_s, 2),
        "초당_입력토큰": round(summary["토큰"]["input_tokens"] / wall_s, 1),
    })
    summary = {"model": args.model, "실행": {**meta, "시작": started_at, "종료": now_utc()}, **summary}

    out.mkdir(parents=True, exist_ok=True)
    with (out / "responses.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (out / "graded.csv").open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글이 깨지지 않도록 BOM 포함
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, summary


def print_summary(summary: dict) -> None:
    for name, v in {**summary["영역별"], **summary["100점 환산"]}.items():
        if v:
            print(f"{name:16s} {v['득점']:>3}/{v['만점']:<3}점  정답 {v['정답수']}/{v['문항수']}  "
                  f"정답 평균확률 {v['정답_평균확률']}")
    for name, g in summary["등급"].items():
        print(f"{name:16s} {g['등급']}등급 (원점수 {g['원점수']}, 표준점수 추정 {g.get('표준점수_추정')})")
    t, c = summary["시간"], summary["비용_USD"]
    if t["호출_시간_s"]:
        lat = t["호출_시간_s"]
        print(f"시간  전체 {t['전체_실행_s']}s · 호출당 중앙값 {lat['중앙값']}s "
              f"(평균 {lat['평균']}s, {lat['최소']}~{lat['최대']}s)")
        print(f"비용  총 ${c['총액']:.6f} · 문항당 ${c['문항당']:.8f}  (입력 ${c['입력_단가_per_1M']}/1M 토큰, 출력 무료)")
    wrong = [f"{x['문항']}번({x['예측']}→{x['정답']})" for x in summary["틀린 문항"]]
    if wrong:
        print("틀린 문항: " + ", ".join(wrong))
    if summary["오류 세트"]:
        print(f"오류로 0점 처리된 세트: {', '.join(summary['오류 세트'])}")


def aggregate(runs: list[tuple[list[dict], dict]], out: Path) -> dict:
    """반복 실행 결과를 회차별·문항별로 집계합니다."""
    run_rows = []
    for i, (_, s) in enumerate(runs, 1):
        row = {"회차": i, "시작": s["실행"]["시작"], "응답_모델": " ".join(s["호출"]["응답_모델"])}
        for label, v in s["100점 환산"].items():
            g = s["등급"].get(label, {})
            row.update({f"{label}_원점수": v.get("득점"), f"{label}_등급": g.get("등급"),
                        f"{label}_표준점수_추정": g.get("표준점수_추정"), f"{label}_백분위_추정": g.get("백분위_추정"),
                        f"{label}_정답수": v.get("정답수")})
        for name in ("독서", "문학", "화법과 작문", "언어와 매체"):
            row[f"{name}_득점"] = s["영역별"][name].get("득점")
        lat = s["시간"]["호출_시간_s"]
        row.update({
            "전체_실행_s": s["시간"]["전체_실행_s"], "호출_중앙값_s": lat.get("중앙값"), "호출_평균_s": lat.get("평균"),
            "호출_최대_s": lat.get("최대"), "ECE": s["보정"].get("ECE"),
            "input_tokens": s["토큰"]["input_tokens"], "output_tokens": s["토큰"]["output_tokens"],
            "비용_USD": s["비용_USD"]["총액"], "오류_세트수": len(s["오류 세트"]),
            "틀린_문항": " ".join(str(x["문항"]) for x in s["틀린 문항"]),
        })
        run_rows.append(row)

    q_rows = []
    for per_run in zip(*(rows for rows, _ in runs)):  # 같은 문항의 회차별 행
        x = per_run[0]
        preds = Counter(r["예측"] for r in per_run)
        pg = [r["정답_확률"] for r in per_run if r["정답_확률"] is not None]
        q_rows.append({
            "set": x["set"], "track": x["track"], "영역": x["영역"], "문항": x["문항"], "정답": x["정답"], "배점": x["배점"],
            "정답_횟수": sum(r["정오"] == "O" for r in per_run), "회차수": len(per_run),
            "정답률": round(sum(r["정오"] == "O" for r in per_run) / len(per_run), 4),
            "최빈_예측": preds.most_common(1)[0][0], "예측_종류수": len(preds),
            "예측_분포": " ".join(f"{k}×{v}" for k, v in preds.most_common()),
            "회차별_예측": "".join(r["예측"] for r in per_run),
            "정답_확률_평균": round(statistics.fmean(pg), 4) if pg else None,
            "정답_확률_표준편차": round(statistics.stdev(pg), 4) if len(pg) > 1 else 0.0,
            "정답_확률_최소": min(pg) if pg else None, "정답_확률_최대": max(pg) if pg else None,
            **{f"{o}_평균": round(statistics.fmean(r[o] for r in per_run if r[o] is not None), 4)
               if any(r[o] is not None for r in per_run) else None for o in OPTIONS},
        })

    for name, rs in (("runs.csv", run_rows), ("questions.csv", q_rows)):
        with (out / name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rs[0].keys()))
            w.writeheader()
            w.writerows(rs)

    agg: dict = {"model": runs[0][1]["model"], "회차수": len(runs), "실행": runs[0][1]["실행"] | {"종료": runs[-1][1]["실행"]["종료"]}}
    agg["원점수"], agg["등급 분포"] = {}, {}
    for label in TRACK_LABEL.values():
        scores = [r[f"{label}_원점수"] for r in run_rows if r[f"{label}_원점수"] is not None]
        grades = [r[f"{label}_등급"] for r in run_rows if r[f"{label}_등급"] is not None]
        if scores:
            agg["원점수"][label] = {**stats(scores, 2), "회차별": scores}
        if grades:
            agg["등급 분포"][label] = {f"{g}등급": n for g, n in sorted(Counter(grades).items())}
    agg["영역별 득점"] = {
        name: {**stats([s["영역별"][name]["득점"] for _, s in runs if s["영역별"][name]], 2),
               "만점": next((s["영역별"][name]["만점"] for _, s in runs if s["영역별"][name]), None)}
        for name in ("독서", "문학", "화법과 작문", "언어와 매체")
    }
    agg["문항 안정성"] = {
        "항상_정답": sum(q["정답_횟수"] == q["회차수"] for q in q_rows),
        "항상_오답": [q["문항"] if q["track"] == "공통" else f"{q['track']} {q['문항']}" for q in q_rows if q["정답_횟수"] == 0],
        "회차마다_갈림": [
            {"문항": q["문항"], "set": q["set"], "배점": q["배점"], "정답": q["정답"],
             "정답_횟수": q["정답_횟수"], "예측_분포": q["예측_분포"]}
            for q in q_rows if 0 < q["정답_횟수"] < q["회차수"]
        ],
        "예측이_바뀐_문항수": sum(q["예측_종류수"] > 1 for q in q_rows),
    }
    agg["보정_ECE"] = stats([s["보정"]["ECE"] for _, s in runs if s["보정"]], 4)
    agg["시간"] = {
        "전체_실행_s": stats([s["시간"]["전체_실행_s"] for _, s in runs]),
        "호출_시간_s(전 회차 합침)": stats([r["호출_시간_s"] for rows, _ in runs
                                      for r in {x["set"]: x for x in rows}.values() if r["호출_시간_s"] is not None]),
    }
    agg["토큰"] = {"input_tokens": sum(r["input_tokens"] for r in run_rows), "output_tokens": sum(r["output_tokens"] for r in run_rows)}
    agg["비용_USD"] = {"입력_단가_per_1M": runs[0][1]["비용_USD"]["입력_단가_per_1M"],
                     "총액": round(sum(r["비용_USD"] for r in run_rows), 6),
                     "회차당": round(statistics.fmean(r["비용_USD"] for r in run_rows), 6)}
    agg["오류 세트"] = {f"run-{i:02d}": s["오류 세트"] for i, (_, s) in enumerate(runs, 1) if s["오류 세트"]}
    (out / "aggregate.json").write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
    return agg


async def main() -> None:
    ap = argparse.ArgumentParser(description="수능 국어를 jev로 풀고 채점합니다.")
    ap.add_argument("--input", type=Path, default=HERE / "data" / "2026_suneung_korean_jev.json")
    ap.add_argument("--answers", type=Path, default=HERE / "data" / "2026_suneung_korean_answers.json")
    ap.add_argument("--cutoffs", type=Path, default=HERE / "data" / "cutoffs", help="등급 컷 TSV 폴더")
    ap.add_argument("--model", default="jev-latest", help="모델 이름 (기본: jev-latest)")
    ap.add_argument("--track", choices=["all", "화작", "언매"], default="all",
                    help="all=전체, 화작=공통+화법과 작문, 언매=공통+언어와 매체")
    ap.add_argument("--only", nargs="*", help="실행할 세트 id (예: 공통_01-03 언매_37)")
    ap.add_argument("--concurrency", type=int, default=4, help="동시 요청 수")
    ap.add_argument("--repeat", type=int, default=1, help="반복 횟수. 2 이상이면 run-NN/ 폴더와 집계 파일을 씁니다.")
    ap.add_argument("--input-price", type=float, default=0.042,
                    help="입력 토큰 100만 개당 단가(USD). 출력 토큰은 무료 (기본: 0.042)")
    ap.add_argument("--out", type=Path, default=HERE / "results")
    ap.add_argument("--list-models", action="store_true", help="사용 가능한 모델만 출력하고 종료")
    args = ap.parse_args()

    if args.list_models:
        async with AsyncTypeSafeClient() as client:
            for m in (await client.models.list()).models:
                print(f"{m.name:24s} {m.release_date}  {m.description}")
        return

    sets = load_sets(args.input, args.track, args.only)
    if not sets:
        sys.exit("실행할 세트가 없습니다. --track / --only 값을 확인하세요.")
    key = json.loads(args.answers.read_text(encoding="utf-8"))
    cutoffs = load_cutoffs(args.cutoffs)
    n_q = sum(len(s["request"]["questions"]) for s in sets)
    meta = {
        "track": args.track, "only": args.only, "concurrency": args.concurrency,
        "세트수": len(sets), "문항수": n_q,
        "sdk": version("typesafe-sdk"), "python": platform.python_version(), "platform": platform.platform(),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "answers_sha256": hashlib.sha256(args.answers.read_bytes()).hexdigest(),
    }
    print(f"모델 {args.model} · {len(sets)}세트 {n_q}문항 · 동시 {args.concurrency}건 · {args.repeat}회")

    runs = []
    for i in range(1, args.repeat + 1):
        out = args.out / f"run-{i:02d}" if args.repeat > 1 else args.out
        if args.repeat > 1:
            print(f"\n── {i}/{args.repeat}회 ──")
        rows, summary = await run_once(args, sets, key, cutoffs, out, {**meta, "회차": i})
        print("\n── 채점 결과 ──")
        print_summary(summary)
        runs.append((rows, summary))

    if args.repeat > 1:
        agg = aggregate(runs, args.out)
        print(f"\n── {args.repeat}회 집계 ──")
        for label, v in agg["원점수"].items():
            print(f"{label:16s} 평균 {v['평균']} (표준편차 {v['표준편차']}, {v['최소']}~{v['최대']})  "
                  f"등급 {agg['등급 분포'].get(label)}  회차별 {v['회차별']}")
        st = agg["문항 안정성"]
        print(f"항상 정답 {st['항상_정답']}문항 · 항상 오답 {st['항상_오답']}")
        print("회차마다 갈린 문항: " + ", ".join(f"{q['문항']}번({q['정답_횟수']}/{args.repeat})" for q in st["회차마다_갈림"]))
        print(f"비용 합계 ${agg['비용_USD']['총액']:.6f}")
        print(f"\n저장: {args.out}/ (run-NN/, runs.csv, questions.csv, aggregate.json)")
    else:
        print(f"\n저장: {args.out}/ (responses.jsonl, graded.csv, summary.json)")


if __name__ == "__main__":
    asyncio.run(main())
