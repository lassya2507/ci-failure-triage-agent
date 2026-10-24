"""Parse pytest output (run with -rfE --tb=short) into structured failures."""
import re
from dataclasses import dataclass, field

_SECTION = re.compile(r"^_{3,} (.+?) _{3,}$")
_FRAME = re.compile(r"^(\S+?\.py):(\d+): in (\S+)$")
_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")


@dataclass
class Failure:
    test_id: str                 # e.g. tests/test_strutils.py::test_slugify
    kind: str                    # "failed" or "error"
    message: str = ""            # the one-line reason from the summary
    error_lines: list = field(default_factory=list)   # the "E   ..." lines
    frames: list = field(default_factory=list)        # (path, line, func), outermost first


def _header_key(test_id: str) -> str:
    """'tests/test_x.py::TestA::test_m[1]' -> 'TestA.test_m[1]' (pytest's section title)."""
    parts = test_id.split("::")
    return ".".join(parts[1:]) if len(parts) > 1 else parts[0]


def parse_pytest_log(log: str) -> list:
    lines = log.splitlines()

    # 1. Split the report into titled sections ("test_foo", "ERROR collecting tests/x.py").
    sections = {}
    title, body = None, []
    for line in lines:
        m = _SECTION.match(line)
        if m:
            if title is not None:
                sections.setdefault(title, body)
            title, body = m.group(1), []
        elif line.startswith("=") and line.endswith("="):
            if title is not None:
                sections.setdefault(title, body)
            title, body = None, []
        elif title is not None:
            body.append(line)
    if title is not None:
        sections.setdefault(title, body)

    # 2. The short summary is the authoritative list of what failed.
    failures = []
    for line in lines:
        m = _SUMMARY.match(line.strip())
        if not m:
            continue
        kind, test_id, msg = m.group(1).lower(), m.group(2), (m.group(3) or "")
        f = Failure(test_id=test_id, kind="failed" if kind == "failed" else "error", message=msg)
        if "::" in test_id:
            key = _header_key(test_id)
            body = sections.get(key) or sections.get("ERROR at setup of " + key) or []
        else:
            body = sections.get("ERROR collecting " + test_id, [])
        for b in body:
            fm = _FRAME.match(b.strip())
            if fm:
                f.frames.append((fm.group(1), int(fm.group(2)), fm.group(3)))
            elif b.startswith("E   "):
                f.error_lines.append(b[4:])
        if not f.message and f.error_lines:
            f.message = f.error_lines[-1]
        failures.append(f)
    return failures
