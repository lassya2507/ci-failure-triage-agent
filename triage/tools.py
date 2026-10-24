"""The tools a triager can use. The rules baseline and the LLM agent call the
same functions, so neither one gets information the other can't see."""
import re
import time

from .case import RepoView
from .tracing import Tracer

MAX_LINES = 150


def _numbered(lines, start):
    return "\n".join(f"{start + i:5d}| {line}" for i, line in enumerate(lines))


class Tools:
    def __init__(self, view: RepoView, tracer: Tracer, max_reruns: int = 3):
        self.view = view
        self.case = view.case
        self.tracer = tracer
        self.max_reruns = max_reruns
        self.reruns_used = 0
        self.rerun_seconds = 0.0   # replayed CI time, added to latency
        self._rerun_pos = {}

    # each public tool goes through here so it shows up in the trace
    def call(self, name: str, **args) -> str:
        t = time.perf_counter()
        fn = getattr(self, "tool_" + name, None)
        if fn is None:
            out = f"error: unknown tool '{name}'"
        else:
            try:
                out = fn(**args)
            except TypeError as e:
                out = f"error: bad arguments for {name}: {e}"
        self.tracer.log("tool", name=name, args=args, ms=round(1000 * (time.perf_counter() - t), 2),
                        out_chars=len(out))
        return out

    def tool_read_log(self, start_line: int = 1, num_lines: int = 120) -> str:
        lines = self.case.log.splitlines()
        start = max(1, int(start_line))
        n = max(1, min(int(num_lines), MAX_LINES))
        chunk = lines[start - 1:start - 1 + n]
        return f"[log has {len(lines)} lines]\n" + _numbered(chunk, start)

    def tool_search_log(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = [(i + 1, l) for i, l in enumerate(self.case.log.splitlines()) if rx.search(l)]
        if not hits:
            return "no matches"
        out = "\n".join(f"{i:5d}| {l}" for i, l in hits[:40])
        return out + (f"\n... {len(hits) - 40} more matches" if len(hits) > 40 else "")

    def tool_read_diff(self) -> str:
        lines = self.case.diff.splitlines()
        text = "\n".join(lines[:400])
        return text + (f"\n... diff truncated ({len(lines)} lines)" if len(lines) > 400 else "")

    def tool_get_changed_functions(self) -> str:
        cf = self.view.changed_functions()
        return "\n".join(f"{path}: {', '.join(funcs) or '(module-level only)'}" for path, funcs in cf.items())

    def tool_read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        if path.startswith("/") or ".." in path:
            return "error: use a repo-relative path like boltons/strutils.py"
        src = self.view.read(path)
        if src is None:
            return f"error: no such file '{path}'"
        lines = src.splitlines()
        start = max(1, int(start_line))
        end = int(end_line) if end_line else start + 99
        end = min(end, start + MAX_LINES - 1, len(lines))
        return f"[{path} has {len(lines)} lines]\n" + _numbered(lines[start - 1:end], start)

    def tool_rerun_test(self, test_id: str) -> str:
        """Reruns are replayed from real recorded reruns of that exact CI run."""
        if self.reruns_used >= self.max_reruns:
            return f"error: rerun budget used up ({self.max_reruns} per case)"
        recorded = self.case.reruns.get(test_id)
        if not recorded:
            return ("error: rerun not available for this test id. Only the failing test ids "
                    "listed in the log summary can be rerun: " + ", ".join(self.case.reruns) )
        pos = self._rerun_pos.get(test_id, 0)
        if pos >= len(recorded):
            return "error: no more reruns available for this test"
        self._rerun_pos[test_id] = pos + 1
        self.reruns_used += 1
        r = recorded[pos]
        self.rerun_seconds += r["seconds"]
        status = "PASSED" if r["passed"] else "FAILED"
        return f"rerun of {test_id}: {status} in {r['seconds']:.2f}s\n{r['tail']}"
