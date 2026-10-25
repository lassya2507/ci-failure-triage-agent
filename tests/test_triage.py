"""Unit tests that run offline: parsing, grounding, and the agent's failure handling
(driven by a scripted fake model instead of a real one)."""
from triage.agent import run_agent
from triage.case import Case, RepoView
from triage.codemap import function_at, function_spans, parse_diff
from triage.llm import FakeBackend, ToolCall
from triage.logparse import parse_pytest_log
from triage.schema import TriageReport, grounding_problems
from triage.tools import Tools
from triage.tracing import Tracer

SRC = '''def helper(x):
    return x + 1


class Box:
    def size(self):
        return 3
'''

DIFF = '''diff --git a/pkg/m.py b/pkg/m.py
--- a/pkg/m.py
+++ b/pkg/m.py
@@ -1,5 +1,5 @@
 def helper(x):
-    return x - 1
+    return x + 1


 class Box:
'''

LOG = '''F                                                                        [100%]
=================================== FAILURES ===================================
__________________________________ test_help ___________________________________
tests/test_m.py:4: in test_help
    assert helper(1) == 0
E   assert 2 == 0
=========================== short test summary info ============================
FAILED tests/test_m.py::test_help - assert 2 == 0
1 failed in 0.01s
'''


def make_case():
    return Case(id="t1", split="dev", truth={"category": "regression", "culprit_file": "pkg/m.py",
                                             "culprit_function": "helper"},
                meta={}, diff=DIFF, log=LOG, files={"pkg/m.py": SRC},
                reruns={"tests/test_m.py::test_help": [{"passed": False, "seconds": 0.5, "tail": "1 failed"}]})


def make_tools(tmp_path):
    return Tools(RepoView(make_case(), tmp_path), Tracer())


def submit(**kw):
    base = {"category": "regression", "culprit_file": "pkg/m.py", "culprit_function": "helper",
            "confidence": "high", "evidence": "e", "suggested_action": "fix"}
    base.update(kw)
    return ToolCall("submit_report", base)


def test_codemap():
    spans = function_spans(SRC)
    assert function_at(spans, 2) == "helper"
    assert function_at(spans, 7) == "Box.size"
    assert parse_diff(DIFF) == {"pkg/m.py": {2}}


def test_logparse():
    (f,) = parse_pytest_log(LOG)
    assert f.test_id == "tests/test_m.py::test_help"
    assert f.frames == [("tests/test_m.py", 4, "test_help")]
    assert f.error_lines == ["assert 2 == 0"]


def test_grounding_rejects_unchanged_function():
    r = TriageReport(category="regression", culprit_file="pkg/m.py", culprit_function="Box.size",
                     confidence="high")
    assert grounding_problems(r, {"pkg/m.py": ["helper"]})
    r.culprit_function = "helper"
    assert not grounding_problems(r, {"pkg/m.py": ["helper"]})


def test_agent_happy_path(tmp_path):
    fake = FakeBackend(script=[[ToolCall("read_diff", {})], [submit()]])
    res = run_agent(make_tools(tmp_path), fake)
    assert not res.fallback and res.report.culprit_function == "helper"
    assert res.llm_calls == 2 and res.tool_calls == 1


def test_agent_retries_after_hallucinated_culprit(tmp_path):
    fake = FakeBackend(script=[[submit(culprit_file="pkg/other.py")], [submit()]])
    res = run_agent(make_tools(tmp_path), fake)
    assert res.invalid_submits == 1 and not res.fallback


def test_agent_falls_back_after_repeated_bad_output(tmp_path):
    bad = submit(category="not_a_category")
    fake = FakeBackend(script=[[bad], [bad], [bad]])
    res = run_agent(make_tools(tmp_path), fake)
    assert res.fallback and res.fallback_reason.startswith("invalid_report")
    assert res.report.category.value == "regression"   # came from the rules baseline


def test_agent_forces_submit_on_last_step(tmp_path):
    fake = FakeBackend(script=[[ToolCall("read_diff", {})]] * 3 + [[submit()]])
    tools = make_tools(tmp_path)
    res = run_agent(tools, fake, max_steps=4)
    assert not res.fallback


def test_rerun_budget(tmp_path):
    tools = make_tools(tmp_path)
    assert "FAILED" in tools.call("rerun_test", test_id="tests/test_m.py::test_help")
    assert tools.call("rerun_test", test_id="tests/test_m.py::test_help").startswith("error")


def test_billing_error_stops_instead_of_falling_back(tmp_path):
    import pytest
    from triage.llm import FatalAPIError

    class Broken:
        model = "fake"

        def start(self, *a):
            return self

        def step(self, **kw):
            raise FatalAPIError("402: credits depleted")

    with pytest.raises(FatalAPIError):
        run_agent(make_tools(tmp_path), Broken())
