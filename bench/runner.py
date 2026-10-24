"""Runs the target library's test suite the way a CI job would."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TARGET_REPO = "https://github.com/mahmoud/boltons.git"
TARGET_COMMIT = "4e5faa3"   # pinned so every case is reproducible
FAKE_ENV_PREFIX = "/opt/ci-runner/site-packages"   # how injected runner hooks appear in logs


def fetch_base(dest=".cache/boltons") -> Path:
    """Clone the target library at the pinned commit (idempotent)."""
    dest = Path(dest)
    if not (dest / "boltons").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", TARGET_REPO, str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", TARGET_COMMIT], check=True)
    return dest


class Runner:
    def __init__(self, base: Path, work: Path):
        self.base = Path(base)
        self.work = Path(work)
        if self.work.exists():
            shutil.rmtree(self.work)
        shutil.copytree(self.base, self.work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self._original = {}
        self.env_dir = None

    # --- file edits ------------------------------------------------------
    def read(self, path):
        return (self.work / path).read_text()

    def write(self, path, text):
        if path not in self._original:
            self._original[path] = self.read(path)
        (self.work / path).write_text(text)

    def reset(self):
        for path, text in self._original.items():
            (self.work / path).write_text(text)
        self._original = {}
        self.env_dir = None

    # --- running ---------------------------------------------------------
    def _env(self):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONHASHSEED", None)
        env.pop("PYTHONPATH", None)
        if self.env_dir:
            env["PYTHONPATH"] = str(self.env_dir)
        return env

    def _clean(self, text):
        text = text.replace(str(self.work.resolve()) + "/", "").replace(str(self.work) + "/", "")
        if self.env_dir:
            text = text.replace(str(self.env_dir), FAKE_ENV_PREFIX)
        return text

    def run(self, targets, timeout=30):
        """Returns (log, returncode, seconds). returncode None means it timed out."""
        cmd = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=short", "-p", "no:cacheprovider", *targets]
        t = time.perf_counter()
        try:
            p = subprocess.run(cmd, cwd=self.work, env=self._env(), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "", None, time.perf_counter() - t
        return self._clean(p.stdout + p.stderr), p.returncode, time.perf_counter() - t

    def run_suite(self):
        return self.run(["tests"])

    def rerun(self, test_id):
        log, rc, secs = self.run([test_id], timeout=60)
        tail = "\n".join(log.strip().splitlines()[-6:])
        return {"passed": rc == 0, "seconds": round(secs, 3), "tail": tail}
