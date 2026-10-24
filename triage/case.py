"""A benchmark case and a read-only view of the repo as CI saw it."""
import json
from dataclasses import dataclass, field
from pathlib import Path

from .codemap import function_at, function_spans, parse_diff


@dataclass
class Case:
    id: str
    split: str                # "dev" or "test"
    truth: dict               # {"category", "culprit_file", "culprit_function"}
    meta: dict                # how the case was generated (never shown to a system)
    diff: str                 # the change under test, unified diff
    log: str                  # full pytest output from the CI run
    files: dict               # path -> content for every file that differs from the base repo
    reruns: dict = field(default_factory=dict)  # test_id -> list of recorded reruns

    @classmethod
    def load(cls, path) -> "Case":
        return cls(**json.loads(Path(path).read_text()))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.__dict__, indent=1))


def load_cases(case_dir, split=None) -> list:
    cases = [Case.load(p) for p in sorted(Path(case_dir).glob("*.json"))]
    return [c for c in cases if split in (None, "all") or c.split == split]


class RepoView:
    """What a triager can see: the base repo plus the files this CI run changed."""

    def __init__(self, case: Case, base_dir):
        self.case = case
        self.base = Path(base_dir)
        self.changed_lines = parse_diff(case.diff)
        self._changed_funcs = None

    def read(self, path: str):
        if path in self.case.files:
            return self.case.files[path]
        p = (self.base / path).resolve()
        if self.base.resolve() not in p.parents or not p.is_file():
            return None
        return p.read_text(errors="replace")

    def changed_functions(self) -> dict:
        """file -> sorted list of functions the diff touches."""
        if self._changed_funcs is None:
            out = {}
            for path, lines in self.changed_lines.items():
                src = self.read(path) or ""
                spans = function_spans(src)
                funcs = {function_at(spans, ln) for ln in lines}
                out[path] = sorted(f for f in funcs if f)
            self._changed_funcs = out
        return self._changed_funcs

    def function_at(self, path: str, line: int):
        src = self.read(path)
        return function_at(function_spans(src), line) if src else None
