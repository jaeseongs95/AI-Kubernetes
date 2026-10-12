#!/usr/bin/env python3
"""Run the AIK-005 storage tests and record observed, source-bound results."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                                encoding="utf-8", timeout=10, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New JSON evidence file; existing files are preserved.")
    args = parser.parse_args()
    # Reserve the requested output before running tests, without overwriting
    # another execution's evidence. Runtime databases remain in OS temp paths.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        started = datetime.now(timezone.utc).isoformat()
        clock = time.monotonic()
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_runtime_storage.py")
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
        passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
        sources = sorted((ROOT / "src" / "aik_runtime").glob("*.py")) + [
            ROOT / "tests" / "test_runtime_storage.py", Path(__file__).resolve(),
            ROOT / ".github" / "workflows" / "runtime-storage.yml",
        ]
        report = {
            "schema_version": 1, "task_id": "AIK-005", "status": "PASS" if passed else "FAIL",
            "scope": "Synthetic runtime metadata storage, real filesystem/concurrency/process crash and migration tests",
            "started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - clock, 3),
            "source_commit": git_output("rev-parse", "HEAD"),
            "working_tree_dirty": None if (dirty := git_output("status", "--porcelain")) is None else bool(dirty),
            "github": {name.lower(): os.environ.get(name) for name in (
                "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "ImageOS", "ImageVersion"
            )},
            "host": {"platform": platform.platform(), "python": platform.python_version(),
                     "sqlite": sqlite3.sqlite_version, "architecture": platform.machine()},
            "tests_run": result.testsRun, "passed": len(result.passed), "failed": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped), "passed_tests": result.passed,
            "failures": [{"test": test.id(), "traceback": detail} for test, detail in result.failures],
            "error_details": [{"test": test.id(), "traceback": detail} for test, detail in result.errors],
            "skip_details": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
            "sources": [{"path": path.relative_to(ROOT).as_posix(),
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sources],
            "qualification": {"product_integration": "NOT_RUN", "clean_windows11_client": "NOT_RUN",
                              "vm_ags_authority": "EXTERNAL", "secret_material_storage": "NOT_SUPPORTED"},
        }
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({"status": report["status"], "tests_run": report["tests_run"],
                      "passed": report["passed"], "failed": report["failed"],
                      "errors": report["errors"], "skipped": report["skipped"]}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
