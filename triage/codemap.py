"""Small helpers for mapping line numbers to functions and diff hunks to lines."""
import ast
import re
from typing import Optional


def function_spans(source: str) -> list:
    """Return (start_line, end_line, qualname) for every function in a file.

    Methods get 'Class.method', nested functions get 'outer.inner'.
    """
    spans = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                spans.append((start, child.end_lineno, qual))
                visit(child, qual + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    try:
        visit(ast.parse(source), "")
    except SyntaxError:
        pass
    return spans


def function_at(spans: list, line: int) -> Optional[str]:
    """Innermost function containing `line`, or None for module-level code."""
    best = None
    for start, end, qual in spans:
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end, qual)
    return best[2] if best else None


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(diff_text: str) -> dict:
    """Map each changed file to the set of new-file line numbers the diff touches.

    Added lines count directly. A deletion has no line in the new file, so we
    record the new-file position where it happened.
    """
    changed = {}
    current = None
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = path[2:] if path.startswith("b/") else path
            changed.setdefault(current, set())
            continue
        if raw.startswith("--- "):
            continue
        m = _HUNK.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if current is None:
            continue
        if raw.startswith("+"):
            changed[current].add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            changed[current].add(max(new_line, 1))
        else:
            new_line += 1
    return changed


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") or name.startswith("test_")
