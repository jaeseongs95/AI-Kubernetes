"""Check one AIK-045 desktop smoke report (CI and local evidence review).

Usage: python -I tools/check_desktop_smoke.py REPORT --expect STATUS --connection STATE

The report is the shell's own output; this script only confirms that it has the
expected verdict, all seven routes with a screenshot each, a sandboxed renderer
and engine answers that match the verdict. Exit 0 when it does, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROUTES = ("connections", "overview", "goals", "workers", "evidence", "usage", "settings")
NOT_CONNECTED = ("connections", "goals", "workers", "evidence", "usage")
BRIDGE = ["engineInfo", "handshake", "readmodelSources", "status"]


def problems(report: dict, report_dir: Path, expect: str, connection: str) -> list[str]:
    found = []
    if report.get("schema_version") != 1 or report.get("scope") != "AIK-045 desktop GUI shell smoke":
        found.append("unexpected report schema or scope")
    if report.get("status") != expect:
        found.append(f"status {report.get('status')!r}, expected {expect!r}")
    if report.get("connection") != connection:
        found.append(f"connection {report.get('connection')!r}, expected {connection!r}")
    if report.get("renderer_node_access") is not False or report.get("window_visible") is not True:
        found.append("renderer must be sandboxed and the window visible")
    if report.get("renderer_bridge") != BRIDGE:
        found.append(f"renderer bridge {report.get('renderer_bridge')!r}")
    routes = report.get("routes")
    if not isinstance(routes, list) or [item.get("route") for item in routes] != list(ROUTES):
        found.append("routes are not the seven expected routes in order")
        routes = []
    for item in routes:
        screenshot = report_dir / str(item.get("screenshot"))
        if item.get("rendered") is not True or not screenshot.is_file() or screenshot.stat().st_size < 1000:
            found.append(f"route {item.get('route')} has no rendered screenshot")
        if item.get("route") in NOT_CONNECTED and item.get("state") != "NOT_CONNECTED":
            found.append(f"route {item.get('route')} must say it is not connected")
    engine = report.get("engine")
    if expect == "PASS" and not (isinstance(engine, dict) and engine.get("ok") is True):
        found.append("PASS needs a real engine.info answer seen by the main process")
    if expect != "PASS" and isinstance(engine, dict) and engine.get("ok") is True:
        found.append("an engine answered, so the smoke must not be blocked")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--expect", required=True, choices=("PASS", "BLOCKED", "FAIL"))
    parser.add_argument("--connection", required=True,
                        choices=("connected", "stale", "unavailable", "incompatible"))
    arguments = parser.parse_args(argv)
    try:
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("report is missing or not JSON")
        return 1
    if not isinstance(report, dict):
        print("report is not an object")
        return 1
    found = problems(report, arguments.report.parent, arguments.expect, arguments.connection)
    for line in found:
        print(line)
    if not found:
        print(f"OK: {report['status']} {report['connection']} on {report.get('platform_family')}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
