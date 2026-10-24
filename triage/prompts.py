"""Prompt and tool declarations for the LLM agent.

The prompt describes the job the way you would brief a new on-call engineer.
It says nothing about how the benchmark was generated.
"""

SYSTEM = """You are triaging a failed CI run for a Python library. A developer pushed a change
(the diff) and the pytest job failed. Decide why it failed.

Categories:
- regression: the change broke library code. Name the changed library function that caused it.
- test_bug: the change broke a test (wrong expected value, wrong input, wrong assertion) while
  the library code is fine. Name the changed test function.
- flaky: a nondeterministic test failed. The failure is unrelated to the change, and the test
  can pass on a rerun without any code change.
- infra: the CI environment itself is broken (missing system modules, resource limits,
  permissions, network). The failure is unrelated to the change.

How to work:
- Look at the evidence before deciding: the failure log, the diff, and the source of the
  failing test and of the changed functions.
- A traceback that ends inside library code is not proof of a regression; check whether
  the changed lines are actually involved.
- Reruns cost CI time. Rerun only when it would change your decision.
- culprit_file and culprit_function must come from the diff (use get_changed_functions).
  For flaky and infra, leave them empty.
- Finish by calling submit_report exactly once. Keep evidence to two or three sentences."""


def user_message(failing_tests: list, changed_files: list, summary_lines: list) -> str:
    return (
        "The CI job failed.\n\n"
        "Short test summary from the log:\n" + "\n".join(summary_lines[:15])
        + (f"\n... and {len(summary_lines) - 15} more" if len(summary_lines) > 15 else "")
        + "\n\nFiles changed by the diff: " + ", ".join(changed_files)
        + "\n\nUse the tools to investigate, then call submit_report."
    )


TOOLS = [
    {"name": "read_log", "description": "Read lines of the full pytest log, with line numbers.",
     "parameters": {"type": "object", "properties": {
         "start_line": {"type": "integer", "description": "1-based line to start at"},
         "num_lines": {"type": "integer", "description": "how many lines (max 150)"}}}},
    {"name": "search_log", "description": "Regex search over the pytest log; returns matching lines.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "read_diff", "description": "Show the full unified diff of the change under test.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_changed_functions", "description": "List, per changed file, the functions the diff touches.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "Read a repo file (as of this CI run) with line numbers.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "repo-relative path, e.g. boltons/strutils.py"},
         "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}},
    {"name": "rerun_test", "description": "Rerun one failing test id on the same runner, without code changes. "
                                          "Costs CI time; at most 3 reruns per job.",
     "parameters": {"type": "object", "properties": {"test_id": {"type": "string"}}, "required": ["test_id"]}},
    {"name": "submit_report", "description": "Submit the final triage report. Call exactly once, at the end.",
     "parameters": {"type": "object", "properties": {
         "category": {"type": "string", "enum": ["regression", "test_bug", "flaky", "infra"]},
         "culprit_file": {"type": "string", "description": "changed file to blame (regression/test_bug only)"},
         "culprit_function": {"type": "string", "description": "changed function to blame, e.g. Cls.method"},
         "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
         "evidence": {"type": "string"},
         "suggested_action": {"type": "string"}},
         "required": ["category", "confidence", "evidence", "suggested_action"]}},
]
