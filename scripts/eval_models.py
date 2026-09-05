"""Brief model eval: which brain should the committee default to?

Each candidate drives the same reduced committee (Buffett, Burry, Wood +
debate room + portfolio manager) on the same ticker. Measured per model:
exact cost (OpenRouter usage delta), wall time, reliability (agents that
fell back to error defaults), and a judge-scored quality rubric. Output:
a ranking by quality with cost alongside, and score-per-dollar.

Run:  poetry run python scripts/eval_models.py
Env:  EVAL_BASE (default http://127.0.0.1:8010), APP_PASSWORD, OPENROUTER_API_KEY
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("EVAL_BASE", "http://127.0.0.1:8010")
AUTH = ("eval", os.environ["APP_PASSWORD"])
TICKER = "AAPL"
ANALYSTS = ["warren_buffett", "michael_burry", "cathie_wood"]
JUDGE = "anthropic/claude-sonnet-5"

CANDIDATES = [
    "qwen/qwen3.7-flash",
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v3.2",
    "google/gemini-3.8-flash",
    "x-ai/grok-4.6",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-fable-5.1",
    "openai/gpt-6-astra",
]

RUBRIC = """You are grading one AI investment committee's output on {ticker}.
Score each dimension 1-10 (10 = excellent):
1. specificity: cites concrete numbers from the provided analysis, not vague claims
2. character: each persona argues in its own recognizable philosophy and voice
3. consistency: each member's signal matches its own reasoning; no self-contradiction
4. debate: the devil's advocate attacks the panel's specific arguments by name

Return JSON only: {{"specificity": n, "character": n, "consistency": n, "debate": n}}"""


def run_committee(model: str) -> dict:
    started = time.time()
    response = requests.post(
        f"{BASE}/analyze/run", auth=AUTH, stream=True, timeout=900,
        json={"ticker": TICKER, "model_name": model, "analysts": ANALYSTS},
    )
    response.raise_for_status()
    data = None
    for raw in response.iter_lines(decode_unicode=True):
        if raw and raw.startswith("data: "):
            payload = json.loads(raw[6:])
            if payload.get("type") == "complete":
                data = payload["data"]
            elif payload.get("type") == "error":
                raise RuntimeError(payload.get("message"))
    if not data:
        raise RuntimeError("stream ended without a complete event")
    return {"data": data, "seconds": time.time() - started}


def reliability(data: dict) -> tuple[int, int]:
    """(members answered cleanly, members total) among LLM members."""
    clean, total = 0, 0
    for agent, signals in data["analyst_signals"].items():
        if agent.startswith("risk_management"):
            continue
        entry = signals.get(TICKER, {})
        total += 1
        reasoning = str(entry.get("reasoning", ""))
        failed = ("Error" in reasoning and "defaulting" in reasoning) or (
            entry.get("confidence") in (0, 0.0) and "Error" in reasoning)
        clean += 0 if failed else 1
    return clean, total


def judge(data: dict) -> dict:
    views = {}
    for agent, signals in data["analyst_signals"].items():
        if agent.startswith("risk_management"):
            continue
        entry = signals.get(TICKER, {})
        views[agent] = {"signal": entry.get("signal"), "confidence": entry.get("confidence"),
                       "reasoning": str(entry.get("reasoning"))[:1200]}
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": JUDGE,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": RUBRIC.format(ticker=TICKER)},
                {"role": "user", "content": json.dumps(views, indent=1)},
            ],
        },
        timeout=180,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    content = content[content.index("{"):content.rindex("}") + 1]
    return json.loads(content)


def main():
    candidates = sys.argv[1:] or CANDIDATES
    results = []
    note_ids = []
    for model in candidates:
        print(f"\n=== {model}", flush=True)
        try:
            run = run_committee(model)
        except Exception as e:
            print(f"  RUN FAILED: {e}")
            results.append({"model": model, "failed": str(e)})
            continue
        data = run["data"]
        if data.get("note_id"):
            note_ids.append(data["note_id"])
        clean, total = reliability(data)
        try:
            scores = judge(data)
            quality = sum(scores.values()) / len(scores)
        except Exception as e:
            print(f"  judge failed: {e}")
            scores, quality = {}, None
        cost = data.get("run_cost") or 0.0
        results.append({
            "model": model, "cost": round(cost, 4), "seconds": round(run["seconds"], 1),
            "clean": f"{clean}/{total}", "scores": scores, "quality": quality,
            "decision": (data["decisions"].get(TICKER) or {}).get("action"),
        })
        print(f"  ${cost:.4f} | {run['seconds']:.0f}s | clean {clean}/{total} | quality {quality} | {scores}")

    print("\n\n==== RANKING (by quality, cost alongside) ====")
    ranked = sorted([r for r in results if r.get("quality")], key=lambda r: -r["quality"])
    for r in ranked:
        per_dollar = r["quality"] / r["cost"] if r["cost"] else float("inf")
        print(f"{r['model']:34} quality {r['quality']:.2f}  ${r['cost']:.4f}  {r['seconds']:>5.0f}s  "
              f"clean {r['clean']}  q/$ {per_dollar:,.0f}  decision {r['decision']}")

    out = "/tmp/eval_results.json"
    json.dump({"results": results, "note_ids": note_ids}, open(out, "w"), indent=1)
    print(f"\nresults + eval note ids -> {out}")


if __name__ == "__main__":
    sys.exit(main())
