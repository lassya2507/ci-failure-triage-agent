"""The LLM agent: a tool-use loop with schema validation, grounding checks,
bounded retries, and a deterministic fallback.

Failure handling, in order:
  * the model answers without a tool call     -> nudge it once, count it
  * submit_report fails schema or grounding   -> send the error back, let it retry (max 2)
  * it runs out of steps                      -> the last turn is forced to submit_report
  * still no valid report, or the API errors  -> fall back to the rules baseline
"""
from dataclasses import dataclass, field

from pydantic import ValidationError

from .logparse import parse_pytest_log
from .prompts import SYSTEM, TOOLS, user_message
from .rules import triage_rules
from .schema import TriageReport, grounding_problems
from .tools import Tools


@dataclass
class AgentResult:
    report: TriageReport
    fallback: bool = False
    fallback_reason: str = ""
    llm_calls: int = 0
    tool_calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    llm_seconds: float = 0.0
    invalid_submits: int = 0
    no_call_turns: int = 0
    tools_used: list = field(default_factory=list)


def run_agent(tools: Tools, backend, max_steps: int = 10, max_invalid: int = 2) -> AgentResult:
    view, tracer = tools.view, tools.tracer
    failures = parse_pytest_log(tools.case.log)
    summary = [l for l in tools.case.log.splitlines() if l.startswith(("FAILED ", "ERROR "))]
    session = backend.start(SYSTEM, user_message([f.test_id for f in failures], sorted(view.changed_lines), summary),
                            TOOLS)
    res = AgentResult(report=None)
    pending, nudge = None, None

    def fallback(reason):
        tracer.log("fallback", reason=reason)
        res.report = triage_rules(tools)
        res.fallback, res.fallback_reason = True, reason
        return res

    for step in range(max_steps):
        force = step == max_steps - 1
        try:
            turn = session.step(tool_results=pending, force_submit=force, nudge=nudge)
        except Exception as e:  # API failure after retries
            if type(e).__name__ == "QuotaExhausted":
                raise
            return fallback(f"api_error: {type(e).__name__}: {str(e)[:200]}")
        pending, nudge = None, None
        res.llm_calls += 1
        res.in_tokens += turn.in_tokens
        res.out_tokens += turn.out_tokens
        res.llm_seconds += turn.seconds
        tracer.log("llm", step=step, in_tokens=turn.in_tokens, out_tokens=turn.out_tokens,
                   seconds=round(turn.seconds, 3), calls=[c.name for c in turn.calls], forced=force)

        if not turn.calls:
            res.no_call_turns += 1
            nudge = "Please continue by calling one of the tools; finish with submit_report."
            continue

        results = []
        for call in turn.calls:
            if call.name != "submit_report":
                res.tool_calls += 1
                res.tools_used.append(call.name)
                results.append((call, tools.call(call.name, **call.args)))
                continue
            args = {k: (v if v != "" else None) for k, v in call.args.items()}
            try:
                report = TriageReport(**args)
                problems = grounding_problems(report, view.changed_functions())
            except ValidationError as e:
                problems = [f"schema error: {err['loc']}: {err['msg']}" for err in e.errors()]
            if not problems:
                if report.category.value in ("flaky", "infra"):
                    report.culprit_file = report.culprit_function = None
                tracer.log("submit", ok=True, report=report.model_dump(mode="json"))
                res.report = report
                return res
            res.invalid_submits += 1
            tracer.log("submit", ok=False, problems=problems)
            if res.invalid_submits > max_invalid:
                return fallback("invalid_report: " + "; ".join(problems)[:200])
            results.append((call, "REJECTED: " + " ".join(problems) + " Fix it and call submit_report again."))
        pending = results

    return fallback("no_valid_report_within_step_budget")
