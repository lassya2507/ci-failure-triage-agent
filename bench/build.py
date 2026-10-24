"""Build the CI-failure benchmark with exact ground truth.

Every case is a real pytest run of a real library (boltons) after a fault we
injected ourselves, so we know the right answer for each one:

  regression  one-token bug in a library function, mixed into a diff with
              behavior-preserving noise edits in other functions
  test_bug    a wrong expected value or input in a test, plus noise edits
  flaky       a nondeterministic test (set ordering / unseeded randomness) that is
              already in the repo; the diff itself is harmless
  infra       the runner environment is broken (missing native module, open-file
              limit, temp-dir permissions); the diff itself is harmless

Usage:  python -m bench.build --out bench/cases
"""
import argparse
import ast
import difflib
import random
import shutil
import tempfile
from pathlib import Path

from triage.case import Case
from triage.logparse import parse_pytest_log

from .edits import apply_line_edits, mutation_candidates, noise_candidates, test_edit_candidates
from .runner import Runner, fetch_base


def make_diff(before: dict, after: dict) -> str:
    out = []
    for path in sorted(after):
        if before[path] == after[path]:
            continue
        out.append(f"diff --git a/{path} b/{path}\n")
        out.extend(difflib.unified_diff(before[path].splitlines(keepends=True), after[path].splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    return "".join(line if line.endswith("\n") else line + "\n" for line in out)


class Builder:
    def __init__(self, base: Path, rng: random.Random):
        self.base = base
        self.rng = rng
        self.runner = Runner(base, Path(tempfile.mkdtemp(prefix="triage_work_")) / "repo")
        self.src_files = sorted(str(p.relative_to(base)) for p in (base / "boltons").glob("*.py")
                                if p.name != "__init__.py" and (base / "tests" / f"test_{p.name}").exists())
        self.test_files = sorted(str(p.relative_to(base)) for p in (base / "tests").glob("test_*.py"))

    # ---------------------------------------------------------------- helpers
    def _noise(self, k, avoid_funcs=(), files=None):
        """Pick k noise edits in distinct functions; returns {path: [(ln, repl, op, func)]}."""
        chosen, used = {}, set(avoid_funcs)
        files = files or self.src_files
        for _ in range(k * 6):
            if sum(len(v) for v in chosen.values()) >= k:
                break
            path = self.rng.choice(files)
            src = self.runner.read(path)
            for ln, repl, op, func in noise_candidates(src, self.rng):
                if (path, func) not in used:
                    used.add((path, func))
                    chosen.setdefault(path, []).append((ln, repl, op, func))
                    break
        return chosen

    def _apply(self, edits_by_file: dict):
        """edits_by_file: path -> list of (lineno, replacement_lines)."""
        before, after = {}, {}
        for path, edits in edits_by_file.items():
            before[path] = self.runner.read(path)
            after[path] = apply_line_edits(before[path], edits)
            ast.parse(after[path])
            self.runner.write(path, after[path])
        return before, after

    def _record_reruns(self, failures):
        """3 reruns of the first failing test, 2 of the second (enough for any triager's budget)."""
        reruns = {}
        for f, n_runs in zip(failures[:2], (3, 2)):
            reruns[f.test_id] = [self.runner.rerun(f.test_id) for _ in range(n_runs)]
        return reruns

    def _noise_is_safe(self, noise):
        if not noise:
            return True
        self._apply({p: [(ln, r) for ln, r, _, _ in es] for p, es in noise.items()})
        _, rc, _ = self.runner.run_suite()
        self.runner.reset()
        return rc == 0

    def _finish(self, cid, truth, meta, before, after, extra_files=None, reruns=None):
        log, rc, secs = self.runner.run_suite()
        failures = parse_pytest_log(log)
        if rc not in (1, 2) or not failures:
            return None
        files = {p: after[p] for p in after if before[p] != after[p]}
        files.update(extra_files or {})
        meta.update(n_failing=len(failures), suite_seconds=round(secs, 2))
        return Case(id=cid, split="dev" if self.rng.random() < 0.25 else "test", truth=truth, meta=meta,
                    diff=make_diff(before, after), log=log, files=files,
                    reruns=reruns if reruns is not None else self._record_reruns(failures))

    # ------------------------------------------------------------- generators
    def regression(self, cid):
        path = self.rng.choice(self.src_files)
        cands = list(mutation_candidates(self.runner.read(path)))
        self.rng.shuffle(cands)
        test_file = "tests/test_" + path.split("/")[-1]
        for ln, new_line, op, func in cands[:25]:
            src = self.runner.read(path)
            old_line = src.splitlines()[ln - 1]
            self.runner.write(path, apply_line_edits(src, [(ln, [new_line])]))
            _, rc, _ = self.runner.run([test_file], timeout=8)
            self.runner.reset()
            if rc not in (1, 2):
                continue  # mutant survived (or hung): not a usable bug
            k = self.rng.choice([0, 1, 1, 2, 2, 3])
            noise = self._noise(k, avoid_funcs=[(path, func)])
            if not self._noise_is_safe(noise):
                continue
            edits = {p: [(l, r) for l, r, _, _ in es] for p, es in noise.items()}
            edits.setdefault(path, []).append((ln, [new_line]))
            before, after = self._apply(edits)
            meta = {"op": op, "line_before": old_line.strip(), "line_after": new_line.strip(),
                    "n_noise": sum(len(v) for v in noise.values()),
                    "noise_same_file": len(noise.get(path, []))}
            case = self._finish(cid, {"category": "regression", "culprit_file": path, "culprit_function": func},
                                meta, before, after)
            self.runner.reset()
            if case:
                return case
        return None

    def test_bug(self, cid):
        path = self.rng.choice(self.test_files)
        cands = list(test_edit_candidates(self.runner.read(path)))
        self.rng.shuffle(cands)
        for ln, new_line, op, func in cands[:25]:
            src = self.runner.read(path)
            old_line = src.splitlines()[ln - 1]
            self.runner.write(path, apply_line_edits(src, [(ln, [new_line])]))
            _, rc, _ = self.runner.run([path], timeout=8)
            self.runner.reset()
            if rc not in (1, 2):
                continue
            noise = self._noise(self.rng.choice([0, 1, 2]))
            if not self._noise_is_safe(noise):
                continue
            edits = {p: [(l, r) for l, r, _, _ in es] for p, es in noise.items()}
            edits.setdefault(path, []).append((ln, [new_line]))
            before, after = self._apply(edits)
            meta = {"op": op, "line_before": old_line.strip(), "line_after": new_line.strip(),
                    "n_noise": sum(len(v) for v in noise.values())}
            case = self._finish(cid, {"category": "test_bug", "culprit_file": path, "culprit_function": func},
                                meta, before, after)
            self.runner.reset()
            if case:
                return case
        return None

    FLAKY_SNIPPETS = {
        "set_order": ['labels = {"alpha", "beta", "gamma"}',
                      'assert ",".join(labels).startswith("alpha")'],
        "unseeded_random": ["import random", "sample = random.sample(range(100), 2)",
                            "assert sample[0] < sample[1]"],
        "shuffle_position": ["import random", "items = list(range(10))", "random.shuffle(items)",
                             "assert items.index(0) < 7"],
    }

    def flaky(self, cid):
        path = self.rng.choice(self.test_files)
        src = self.runner.read(path)
        tests = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef) and n.name.startswith("test") and not n.args.args]
        if not tests:
            return None
        node = self.rng.choice(tests)
        kind = self.rng.choice(list(self.FLAKY_SNIPPETS))
        first = node.body[0]
        is_doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
        anchor = first.end_lineno if is_doc else first.lineno - 1
        indent = " " * (node.body[1 if is_doc and len(node.body) > 1 else 0].col_offset)
        lines = src.splitlines()
        lines[anchor:anchor] = [indent + s for s in self.FLAKY_SNIPPETS[kind]]
        flaky_src = "\n".join(lines) + "\n"
        noise = self._noise(self.rng.choice([1, 1, 2]))
        if not noise:
            return None
        before, after = self._apply({p: [(l, r) for l, r, _, _ in es] for p, es in noise.items()})
        self.runner.write(path, flaky_src)    # part of the repo state, not of the diff
        test_id = f"{path}::{node.name}"
        for _ in range(12):                    # sample CI runs until the flaky test fails
            log, rc, secs = self.runner.run_suite()
            failures = parse_pytest_log(log)
            if rc == 1 and [f.test_id for f in failures] == [test_id]:
                case = Case(id=cid, split="dev" if self.rng.random() < 0.25 else "test",
                            truth={"category": "flaky", "culprit_file": None, "culprit_function": None},
                            meta={"op": kind, "flaky_test": test_id,
                                  "n_noise": sum(len(v) for v in noise.values()), "n_failing": 1,
                                  "suite_seconds": round(secs, 2)},
                            diff=make_diff(before, after), log=log,
                            files={**{p: after[p] for p in after}, path: flaky_src},
                            reruns={test_id: [self.runner.rerun(test_id) for _ in range(3)]})
                self.runner.reset()
                return case
        self.runner.reset()
        return None

    INFRA_HOOKS = {
        "missing_module": '''
import sys
class _Finder:
    def find_spec(self, name, path=None, target=None):
        if name == {mod!r}:
            raise ModuleNotFoundError("No module named {mod!r}", name=name)
        return None
sys.meta_path.insert(0, _Finder())
''',
        "fd_limit": '''
import errno, os, socket
class _LimitedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
socket.socket = _LimitedSocket
''',
        "tmp_permission": '''
import errno, os, tempfile
_mkdir = os.mkdir
def _guarded_mkdir(path, *args, **kwargs):
    if str(path).startswith(tempfile.gettempdir()):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
    return _mkdir(path, *args, **kwargs)
os.mkdir = _guarded_mkdir
''',
    }

    def infra(self, cid):
        kind = self.rng.choice(["missing_module", "missing_module", "fd_limit", "tmp_permission"])
        mod = self.rng.choice(["zlib", "unicodedata", "mailbox"])
        env_dir = Path(tempfile.mkdtemp(prefix="runner_env_"))
        (env_dir / "sitecustomize.py").write_text(self.INFRA_HOOKS[kind].format(mod=mod) if kind == "missing_module"
                                                  else self.INFRA_HOOKS[kind])
        noise = self._noise(self.rng.choice([1, 1, 2]))
        if not noise or not self._noise_is_safe(noise):
            return None
        before, after = self._apply({p: [(l, r) for l, r, _, _ in es] for p, es in noise.items()})
        self.runner.env_dir = env_dir
        meta = {"op": kind + (f":{mod}" if kind == "missing_module" else ""),
                "n_noise": sum(len(v) for v in noise.values())}
        case = self._finish(cid, {"category": "infra", "culprit_file": None, "culprit_function": None},
                            meta, before, after)
        self.runner.reset()
        shutil.rmtree(env_dir, ignore_errors=True)
        return case


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench/cases")
    ap.add_argument("--base", default=".cache/boltons")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--counts", default="regression=60,test_bug=24,flaky=20,infra=16")
    ap.add_argument("--start", type=int, default=0, help="first case index (to extend an existing set)")
    args = ap.parse_args()

    base = fetch_base(args.base)
    rng = random.Random(args.seed)
    builder = Builder(base, rng)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    counts = dict((k, int(v)) for k, v in (kv.split("=") for kv in args.counts.split(",")))
    for kind, n in counts.items():
        made, tries = 0, 0
        while made < n and tries < n * 6:
            tries += 1
            cid = f"{kind}-{args.start + made:03d}"
            case = getattr(builder, kind)(cid)
            if case is None:
                continue
            case.save(out / f"{cid}.json")
            made += 1
            print(f"{cid}  split={case.split}  op={case.meta.get('op')}  failing={case.meta.get('n_failing')}",
                  flush=True)
        print(f"== {kind}: {made}/{n} built in {tries} tries", flush=True)


if __name__ == "__main__":
    main()
