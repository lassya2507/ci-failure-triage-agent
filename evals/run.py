"""Run one triage system over the benchmark and write one JSON line per case.

  python -m evals.run --system rules --split test
  python -m evals.run --system agent --split test --model gemini-3.1-flash-lite

Agent runs append to their results file and skip cases already done, so a run
that hits the free-tier daily quota can simply be started again the next day.
"""
import argparse
import json
import os
import time
from pathlib import Path

from triage.agent import run_agent
from triage.case import RepoView, load_cases
from triage.llm import QuotaExhausted, cost_usd
from triage.rules import triage_rules
from triage.tools import Tools
from triage.tracing import Tracer


def run_case(case, base, system, backend, trace_dir):
    tracer = Tracer(Path(trace_dir) / f"{case.id}.jsonl")
    tools = Tools(RepoView(case, base), tracer)
    t = time.perf_counter()
    waited0 = getattr(backend, "waited", 0.0)
    tracer.log("case_start", case=case.id, system=system)
    row = {"id": case.id, "system": system}
    if system == "rules":
        report = triage_rules(tools)
        row.update(llm_calls=0, in_tokens=0, out_tokens=0, llm_seconds=0.0, fallback=False,
                   invalid_submits=0, tool_calls=tools.reruns_used, tools_used=[])
    else:
        res = run_agent(tools, backend)
        report = res.report
        row.update(llm_calls=res.llm_calls, in_tokens=res.in_tokens, out_tokens=res.out_tokens,
                   llm_seconds=round(res.llm_seconds, 3), fallback=res.fallback, fallback_reason=res.fallback_reason,
                   invalid_submits=res.invalid_submits, no_call_turns=res.no_call_turns,
                   tool_calls=res.tool_calls, tools_used=res.tools_used)
    waited = getattr(backend, "waited", 0.0) - waited0
    compute = time.perf_counter() - t - row["llm_seconds"] - waited
    row.update(
        report=report.model_dump(mode="json"),
        reruns=tools.reruns_used,
        rerun_seconds=round(tools.rerun_seconds, 3),
        compute_seconds=round(compute, 4),
        waited_seconds=round(waited, 2),
        # active latency = our code + time inside LLM calls + replayed CI rerun time.
        # Free-tier rate-limit waits are excluded and reported separately.
        latency_seconds=round(compute + row["llm_seconds"] + tools.rerun_seconds, 3),
        cost_usd=cost_usd(getattr(backend, "model", ""), row["in_tokens"], row["out_tokens"]) if backend else 0.0,
    )
    tracer.log("case_end", report=row["report"], latency_seconds=row["latency_seconds"])
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["rules", "agent"], required=True)
    ap.add_argument("--split", default="test", choices=["dev", "test", "all"])
    ap.add_argument("--cases", default="bench/cases")
    ap.add_argument("--base", default=".cache/boltons")
    ap.add_argument("--model", default="gemini-3.1-flash-lite")
    ap.add_argument("--rpm", type=float, default=8, help="client-side request cap for the free tier")
    ap.add_argument("--thinking-budget", type=int, default=None)
    ap.add_argument("--limit", type=int, default=0, help="only run the first N cases (smoke test)")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    cases = load_cases(args.cases, args.split)
    if args.limit:
        cases = cases[:args.limit]
    name = "rules" if args.system == "rules" else f"agent_{args.model}"
    out_path = Path(args.out) / f"{name}_{args.split}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    backend = None
    if args.system == "agent":
        from triage.llm import GeminiBackend
        backend = GeminiBackend(args.model, os.environ["GEMINI_API_KEY"], rpm=args.rpm,
                                thinking_budget=args.thinking_budget)
    elif out_path.exists():
        out_path.unlink()   # rules are deterministic and fast; always rerun them fully

    done = set()
    if out_path.exists():
        done = {json.loads(l)["id"] for l in out_path.read_text().splitlines() if l.strip()}
    todo = [c for c in cases if c.id not in done]
    print(f"{name}: {len(todo)} cases to run ({len(done)} already done)", flush=True)

    t0 = time.time()
    for i, case in enumerate(todo, 1):
        try:
            row = run_case(case, args.base, args.system, backend, Path(args.out) / "traces" / name)
        except QuotaExhausted as e:
            print(f"\nDaily quota reached after {i - 1} cases. Run the same command later to resume.\n{e}")
            break
        with out_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        r = row["report"]
        ok = "ok " if r["category"] == case.truth["category"] else "XX "
        print(f"[{i:3d}/{len(todo)}] {ok}{case.id:16s} -> {r['category']:10s} {r.get('culprit_function') or '':28s}"
              f" {row['latency_seconds']:6.2f}s{'  FALLBACK' if row.get('fallback') else ''}", flush=True)
    if backend:
        print(f"wall time {time.time() - t0:.0f}s, of which rate-limit/retry waiting {backend.waited:.0f}s")


if __name__ == "__main__":
    main()
