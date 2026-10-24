"""Score result files against ground truth and write results/summary.md.

  python -m evals.report --split test

Also builds the hybrid systems from existing results, with no extra LLM calls:
  hybrid_low    keep the rules answer unless rules said confidence=low
  hybrid_med    escalate rules answers with confidence low or medium
An escalated case takes the agent's answer and pays the agent's cost and latency
on top of the rules' (the rules always run first).
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from triage.case import load_cases
from triage.schema import NEEDS_CULPRIT, Category, names_match

CATS = ["regression", "test_bug", "flaky", "infra"]


def load_rows(path):
    return {r["id"]: r for r in (json.loads(l) for l in Path(path).read_text().splitlines() if l.strip())}


def grade(case, report):
    cat_ok = report["category"] == case.truth["category"]
    needs = Category(case.truth["category"]) in NEEDS_CULPRIT
    culprit_ok = None
    if needs:
        culprit_ok = cat_ok and report.get("culprit_file") == case.truth["culprit_file"] and \
            names_match(report.get("culprit_function"), case.truth["culprit_function"])
    full_ok = cat_ok and (culprit_ok if needs else True)
    return cat_ok, culprit_ok, full_ok


def hybrid(rules, agent, escalate_on):
    out = {}
    for cid, r in rules.items():
        if r["report"]["confidence"] in escalate_on and cid in agent:
            a = agent[cid]
            merged = dict(a)
            merged.update(
                escalated=True,
                latency_seconds=r["latency_seconds"] + a["latency_seconds"],
                reruns=r["reruns"] + a["reruns"],
                cost_usd=(a["cost_usd"] or 0.0) if a["cost_usd"] is not None else None)
            out[cid] = merged
        else:
            out[cid] = dict(r, escalated=False)
    return out


def pct(x, n):
    return f"{100 * x / n:.1f}%" if n else "n/a"


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(name, rows, cases):
    by_id = {c.id: c for c in cases}
    ids = [cid for cid in rows if cid in by_id]
    n = len(ids)
    cat = cul = full = cul_n = 0
    per_cat = defaultdict(lambda: [0, 0])
    confusion = Counter()
    for cid in ids:
        c, r = by_id[cid], rows[cid]
        cat_ok, culprit_ok, full_ok = grade(c, r["report"])
        cat += cat_ok
        full += full_ok
        if culprit_ok is not None:
            cul_n += 1
            cul += culprit_ok
        per_cat[c.truth["category"]][0] += full_ok
        per_cat[c.truth["category"]][1] += 1
        confusion[(c.truth["category"], r["report"]["category"])] += 1
    lat = [rows[i]["latency_seconds"] for i in ids]
    costs = [rows[i]["cost_usd"] for i in ids if rows[i].get("cost_usd") is not None]
    s = {
        "system": name, "n": n,
        "category_acc": pct(cat, n), "culprit_acc": pct(cul, cul_n), "fully_correct": pct(full, n),
        "lat_p50_s": round(quantile(lat, 0.5), 2), "lat_p95_s": round(quantile(lat, 0.95), 2),
        "cost_per_case_usd": f"{sum(costs) / len(costs):.5f}" if costs else "n/a",
        "llm_calls_per_case": round(sum(rows[i].get("llm_calls", 0) for i in ids) / max(n, 1), 2),
        "tokens_per_case": round(sum(rows[i].get("in_tokens", 0) + rows[i].get("out_tokens", 0) for i in ids) / max(n, 1)),
        "reruns_per_case": round(sum(rows[i]["reruns"] for i in ids) / max(n, 1), 2),
        "fallback_rate": pct(sum(bool(rows[i].get("fallback")) for i in ids), n),
        "invalid_submit_rate": pct(sum(rows[i].get("invalid_submits", 0) > 0 for i in ids), n),
        "escalated": pct(sum(bool(rows[i].get("escalated")) for i in ids), n) if any("escalated" in rows[i] for i in ids) else "",
    }
    return s, per_cat, confusion


def breakdown_regressions(name, rows, cases):
    """Regression accuracy split by whether the traceback points at the changed line."""
    from triage.case import RepoView
    from triage.logparse import parse_pytest_log
    groups = defaultdict(lambda: [0, 0])
    for c in cases:
        if c.truth["category"] != "regression" or c.id not in rows:
            continue
        view = RepoView(c, ".cache/boltons")
        frames = [fr for f in parse_pytest_log(c.log) for fr in f.frames]
        on_line = any(fr[0] in view.changed_lines and any(abs(fr[1] - l) <= 1 for l in view.changed_lines[fr[0]])
                      for fr in frames)
        key = "traceback hits changed line" if on_line else "traceback misses changed line"
        groups[key][0] += grade(c, rows[c.id]["report"])[2]
        groups[key][1] += 1
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test")
    ap.add_argument("--cases", default="bench/cases")
    ap.add_argument("--results", default="results")
    args = ap.parse_args()

    cases = load_cases(args.cases, args.split)
    res_dir = Path(args.results)
    systems = {}
    rules_path = res_dir / f"rules_{args.split}.jsonl"
    if rules_path.exists():
        systems["rules"] = load_rows(rules_path)
    for p in sorted(res_dir.glob(f"agent_*_{args.split}.jsonl")):
        name = p.name[: -len(f"_{args.split}.jsonl")]
        systems[name] = load_rows(p)
        if "rules" in systems:
            systems[f"hybrid_low({name[6:]})"] = hybrid(systems["rules"], systems[name], {"low"})
            systems[f"hybrid_med({name[6:]})"] = hybrid(systems["rules"], systems[name], {"low", "medium"})

    lines = [f"# Results ({args.split} split, {len(cases)} cases)", "",
             "Truth mix: " + ", ".join(f"{k}={v}" for k, v in Counter(c.truth['category'] for c in cases).items()), ""]
    summaries = []
    for name, rows in systems.items():
        s, per_cat, confusion = summarize(name, rows, cases)
        summaries.append((s, per_cat, confusion, rows))
    if not summaries:
        print("no result files found")
        return
    cols = list(summaries[0][0])
    lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for s, *_ in summaries:
        lines.append("| " + " | ".join(str(s[c]) for c in cols) + " |")

    lines += ["", "## Fully correct, by true category", "",
              "| system | " + " | ".join(CATS) + " |", "|---|" + "---|" * len(CATS)]
    for s, per_cat, _, _ in summaries:
        lines.append(f"| {s['system']} | " + " | ".join(
            f"{per_cat[c][0]}/{per_cat[c][1]}" for c in CATS) + " |")

    lines += ["", "## Regressions: does the traceback point at the bug?", "",
              "| system | traceback hits changed line | traceback misses changed line |", "|---|---|---|"]
    for s, _, _, rows in summaries:
        g = breakdown_regressions(s["system"], rows, cases)
        cells = [f"{g[k][0]}/{g[k][1]}" for k in ("traceback hits changed line", "traceback misses changed line")]
        lines.append(f"| {s['system']} | " + " | ".join(cells) + " |")

    lines += ["", "## Confusion matrices (rows = truth, columns = predicted)", ""]
    for s, _, confusion, _ in summaries:
        lines += [f"**{s['system']}**", "", "| truth \\ pred | " + " | ".join(CATS) + " |", "|---|" + "---|" * len(CATS)]
        for t in CATS:
            lines.append(f"| {t} | " + " | ".join(str(confusion[(t, p)]) for p in CATS) + " |")
        lines.append("")

    out = res_dir / f"summary_{args.split}.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
