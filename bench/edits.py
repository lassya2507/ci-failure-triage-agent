"""Source edits used to build the benchmark.

Three kinds:
  * mutations   - a one-token bug in library code (the ground-truth regression)
  * test edits  - a wrong expected value or input in a test (ground-truth test bug)
  * noise       - behavior-preserving edits, so a diff looks like a real commit
                  that touches several functions, not just the broken one
Every edit returns the new file text plus the 1-based line it changed.
"""
import ast
import random

from triage.codemap import function_at, function_spans

_SWAP_CMP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==",
             " is not ": " is ", " is ": " is not ", " not in ": " in ", " in ": " not in "}
_CMP_NODE = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
             ast.Is: " is ", ast.IsNot: " is not ", ast.In: " in ", ast.NotIn: " not in "}


def _replace_cols(line: str, start: int, end: int, new: str) -> str:
    """ast column offsets are UTF-8 byte offsets, so slice the encoded line."""
    b = line.encode()
    return (b[:start] + new.encode() + b[end:]).decode()


def _segment(line, start, end):
    return line.encode()[start:end].decode()


def _single_line(node):
    return getattr(node, "lineno", None) and node.lineno == node.end_lineno


def mutation_candidates(src: str):
    """Yield (lineno, new_line, op_name, function_qualname) for one-token bugs."""
    lines = src.splitlines()
    tree = ast.parse(src)
    spans = function_spans(src)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if not _single_line(node):
            continue
        func = function_at(spans, node.lineno)
        if not func:
            continue
        ln = node.lineno
        line = lines[ln - 1]
        try:
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP_NODE:
                a, b = node.left.end_col_offset, node.comparators[0].col_offset
                seg = _segment(line, a, b)
                op = _CMP_NODE[type(node.ops[0])]
                if seg.strip() == op.strip():
                    new_seg = seg.replace(op.strip(), _SWAP_CMP[op].strip(), 1)
                    yield ln, _replace_cols(line, a, b, new_seg), "compare_swap", func
            elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
                a, b = node.values[0].end_col_offset, node.values[1].col_offset
                seg = _segment(line, a, b)
                word = "and" if isinstance(node.op, ast.And) else "or"
                other = "or" if word == "and" else "and"
                if seg.strip() == word:
                    yield ln, _replace_cols(line, a, b, seg.replace(word, other)), "boolop_swap", func
            elif (isinstance(node, ast.Constant) and type(node.value) is int and 0 <= node.value <= 16
                  and id(node) not in docstrings):
                new = node.value + 1
                yield ln, _replace_cols(line, node.col_offset, node.end_col_offset, str(new)), "int_plus_one", func
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                seg = _segment(line, node.col_offset, node.operand.col_offset)
                if seg.strip() == "not":
                    yield ln, _replace_cols(line, node.col_offset, node.operand.col_offset, ""), "drop_not", func
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                a, b = node.left.end_col_offset, node.right.col_offset
                seg = _segment(line, a, b)
                old, new = ("+", "-") if isinstance(node.op, ast.Add) else ("-", "+")
                if seg.strip() == old:
                    yield ln, _replace_cols(line, a, b, seg.replace(old, new)), "arith_swap", func
            elif (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
                  and isinstance(node.value.value, bool)):
                v = node.value
                yield ln, _replace_cols(line, v.col_offset, v.end_col_offset, str(not v.value)), "flip_return", func
        except (UnicodeDecodeError, IndexError):
            continue


def test_edit_candidates(src: str):
    """Yield (lineno, new_line, op_name, test_function) for wrong-test edits."""
    lines = src.splitlines()
    tree = ast.parse(src)
    spans = function_spans(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
            continue
        qual = function_at(spans, node.lineno)
        for stmt in ast.walk(node):
            if not _single_line(stmt) or not isinstance(stmt, (ast.Assert, ast.Assign, ast.Expr)):
                continue
            ln = stmt.lineno
            line = lines[ln - 1]
            for sub in ast.walk(stmt):
                if not _single_line(sub):
                    continue
                if isinstance(sub, ast.Constant) and type(sub.value) is int and -5 < sub.value < 1000:
                    op = "expected_int" if "assert" in line.lower() else "input_int"
                    yield ln, _replace_cols(line, sub.col_offset, sub.end_col_offset, str(sub.value + 1)), op, qual
                elif (isinstance(sub, ast.Constant) and isinstance(sub.value, str) and 0 < len(sub.value) < 40
                      and isinstance(stmt, ast.Assert)):
                    seg = _segment(line, sub.col_offset, sub.end_col_offset)
                    if seg[-1] in "'\"" and not seg.startswith(("b", "r", "f", "u", "B", "R", "F", "U")):
                        yield ln, _replace_cols(line, sub.end_col_offset - 1, sub.end_col_offset - 1, "s"), "expected_str", qual
            if isinstance(stmt, ast.Assert) and isinstance(stmt.test, ast.Compare) and len(stmt.test.ops) == 1 \
                    and isinstance(stmt.test.ops[0], ast.Eq):
                a, b = stmt.test.left.end_col_offset, stmt.test.comparators[0].col_offset
                seg = _segment(line, a, b)
                if seg.strip() == "==":
                    yield ln, _replace_cols(line, a, b, seg.replace("==", "!=")), "assert_flip", qual


_COMMENTS = [
    "# keep the common case first, it is the hot path",
    "# NOTE: callers rely on this staying cheap",
    "# see the module docstring for the edge cases",
    "# small cleanup, behavior unchanged",
    "# TODO: revisit once the py2 shims are gone",
]


def noise_candidates(src: str, rng: random.Random):
    """Yield (lineno, replacement_lines, op_name, function) behavior-preserving edits."""
    lines = src.splitlines()
    tree = ast.parse(src)
    spans = function_spans(src)
    funcs = {id(n): n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out = []
    for f in funcs.values():
        qual = function_at(spans, f.lineno)
        body_src = "\n".join(lines[f.lineno - 1:f.end_lineno])
        for stmt in ast.walk(f):
            if not isinstance(stmt, ast.stmt) or not _single_line(stmt):
                continue
            if function_at(spans, stmt.lineno) != qual:
                continue  # belongs to a nested function, handled on its own
            line = lines[stmt.lineno - 1]
            indent = line[: len(line) - len(line.lstrip())]
            if line.strip().startswith(("@", "else", "elif", "except", "finally")) or line.rstrip().endswith(":"):
                continue
            if (isinstance(stmt, ast.Return) and stmt.value is not None
                    and not isinstance(stmt.value, (ast.Constant, ast.Name))
                    and line.strip().startswith("return ") and not line.rstrip().endswith("\\")):
                var = "result" if "result" not in body_src else "out"
                if var in body_src:
                    continue
                expr = line.strip()[len("return "):]
                out.append((stmt.lineno, [f"{indent}{var} = {expr}", f"{indent}return {var}"], "return_to_local", qual))
            elif rng.random() < 0.15:
                out.append((stmt.lineno, [f"{indent}{rng.choice(_COMMENTS)}", line], "add_comment", qual))
    rng.shuffle(out)
    return out


def apply_line_edits(src: str, edits: list) -> str:
    """edits: list of (lineno, replacement_lines). Applied bottom-up so line numbers stay valid."""
    lines = src.splitlines()
    for ln, repl in sorted(edits, key=lambda e: -e[0]):
        lines[ln - 1:ln] = repl
    return "\n".join(lines) + ("\n" if src.endswith("\n") else "")
