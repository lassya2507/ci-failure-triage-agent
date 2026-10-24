"""Deterministic baseline: the triage rules a CI engineer would write by hand.

Order of checks (first match wins):
  1. known environment-error signature, not raised from changed code -> infra
  2. traceback frame on a changed line                 -> regression / test_bug (high)
  3. traceback passes through a changed function       -> regression / test_bug (medium)
  4. the failing test function itself was changed      -> test_bug (medium)
  5. rerun the failing test; it passes                 -> flaky (medium)
  6. otherwise guess regression, pick the changed function whose name looks most
     like the failing test's name                       -> regression (low)

The confidence label matters: the hybrid system escalates low-confidence cases to the agent.
"""
import re

from .codemap import is_test_file
from .logparse import parse_pytest_log
from .schema import Category, TriageReport
from .tools import Tools

INFRA_SIGNATURES = [
    (r"ModuleNotFoundError: No module named '(?!boltons)[\w.]+'", "a required module is missing on the runner"),
    (r"No space left on device|\[Errno 28\]", "the runner is out of disk space"),
    (r"Too many open files|\[Errno 24\]", "the runner hit its open-file limit"),
    (r"Temporary failure in name resolution|Name or service not known|gaierror", "DNS is broken on the runner"),
    (r"PermissionError: \[Errno 13\]", "the runner lacks a filesystem permission"),
    (r"MemoryError|Killed|Timeout", "the runner ran out of resources"),
]


def _touches_changed_line(frame, changed_lines, slack=1):
    path, line, _ = frame
    lines = changed_lines.get(path)
    return bool(lines) and any(abs(line - c) <= slack for c in lines)


def _tokens(name):
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t != "test"}


def triage_rules(tools: Tools) -> TriageReport:
    view = tools.view
    failures = parse_pytest_log(tools.case.log)
    changed_lines = view.changed_lines
    changed_funcs = view.changed_functions()

    def report(cat, conf, why, path=None, func=None):
        actions = {
            Category.REGRESSION: f"Fix {func} in {path} (introduced by this change).",
            Category.TEST_BUG: f"Fix the test {func} in {path}; the library code looks fine.",
            Category.FLAKY: "Rerun the job and file a flaky-test ticket; do not block this change.",
            Category.INFRA: "Fix or replace the CI runner; do not block this change.",
        }
        return TriageReport(category=cat, culprit_file=path, culprit_function=func,
                            confidence=conf, evidence=why, suggested_action=actions[cat])

    if not failures:
        return report(Category.INFRA, "low", "no failures could be parsed from the log")

    # 1. environment signatures
    for f in failures:
        text = f.message + "\n" + "\n".join(f.error_lines)
        for pattern, meaning in INFRA_SIGNATURES:
            if re.search(pattern, text):
                if not any(_touches_changed_line(fr, changed_lines) for fr in f.frames):
                    return report(Category.INFRA, "high", f"{f.test_id}: {meaning} ({f.message[:120]})")

    # 2. a frame sits on a changed line (deepest frame first)
    for f in failures:
        for fr in reversed(f.frames):
            if _touches_changed_line(fr, changed_lines):
                path, line, _ = fr
                func = view.function_at(path, line)
                cat = Category.TEST_BUG if is_test_file(path) else Category.REGRESSION
                return report(cat, "high", f"{f.test_id} fails at changed line {path}:{line}", path, func)

    # 3. a frame runs through a changed function
    for f in failures:
        for fr in reversed(f.frames):
            path, line, _ = fr
            func = view.function_at(path, line)
            if func and func in changed_funcs.get(path, []):
                cat = Category.TEST_BUG if is_test_file(path) else Category.REGRESSION
                return report(cat, "medium", f"{f.test_id} runs through changed function {func}", path, func)

    # 4. the failing test function was edited
    for f in failures:
        path = f.test_id.split("::")[0]
        test_func = ".".join(f.test_id.split("::")[1:]).split("[")[0]
        if test_func and test_func in changed_funcs.get(path, []):
            return report(Category.TEST_BUG, "medium", f"the failing test {test_func} was edited", path, test_func)

    # 5. rerun the first failing test (up to 2 tries)
    first = failures[0].test_id
    for _ in range(2):
        out = tools.call("rerun_test", test_id=first)
        if "PASSED" in out.splitlines()[0]:
            return report(Category.FLAKY, "medium", f"{first} passed on rerun without any code change")
        if out.startswith("error"):
            break

    # 6. guess a regression in the changed library code
    src_funcs = [(p, fn) for p, fns in changed_funcs.items() if not is_test_file(p) for fn in fns]
    if not src_funcs:
        return report(Category.FLAKY, "low", "no library code changed and the failure did not match any rule")
    test_tokens = set()
    for f in failures:
        test_tokens |= _tokens(f.test_id.split("::")[-1])
    best = max(src_funcs, key=lambda pf: len(_tokens(pf[1].split(".")[-1]) & test_tokens))
    return report(Category.REGRESSION, "low",
                  f"no traceback link to the change; guessed {best[1]} by name overlap with the failing tests",
                  best[0], best[1])
