"""AIK-008 offline historical and opt-in installed upstream diagnostic CLI.

Never opens a provider account, reads API keys or runs arbitrary project tasks.
Use --live only with an explicitly supplied installed VM executable and AGS root;
the existing upstream inspector runs conformance inside a fresh temp workspace.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from aik_compat.diagnostics import DiagnosticError, inspect, read_json

BASELINE = REPO / "contracts/upstream/baseline.json"
MATRIX = REPO / "contracts/upstream/support-matrix.json"
INTEGRATION = REPO / "contracts/upstream/integration-contract.json"
REFERENCE = REPO / "contracts/upstream/installed-inspection.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspection", type=Path, default=REFERENCE,
                        help="Existing report (always historical, never a live qualification)")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument("--integration", type=Path, default=INTEGRATION)
    parser.add_argument("--live", action="store_true",
                        help="Execute the installed upstream metadata/A2 conformance probe")
    parser.add_argument("--ags-root", type=Path)
    parser.add_argument("--vm-python", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-blocked", action="store_true",
                        help="Return zero for a valid blocked diagnosis (CI snapshot review only)")
    args = parser.parse_args(argv)

    if args.live and (not args.ags_root or not args.vm_python or not args.node):
        parser.error("--live requires --ags-root, --vm-python and --node")
    if not args.live and any((args.ags_root, args.vm_python, args.node)):
        parser.error("Installed artifacts can only be provided with --live")
    if args.output:
        resolved = args.output.resolve()
        # Do not place operational/raw installer information in Git or the
        # canonical development ledger, even with an explicit custom output.
        if resolved.is_relative_to(REPO):
            parser.error("output must be outside the source checkout")

    try:
        baseline, baseline_bytes = read_json(args.baseline)
        matrix, matrix_bytes = read_json(args.matrix)
        integration, integration_bytes = read_json(args.integration)
        source = "live" if args.live else "historical"
        if args.live:
            # Imported from this tools directory; keeps existing non-invasive
            # isolation, pinned installed-byte inspection, and bounded timeout.
            from inspect_upstream import inspect_upstream
            raw_report = inspect_upstream(
                args.ags_root.resolve(strict=True),
                str(args.vm_python.resolve(strict=True)),
                str(args.node.resolve(strict=True)), baseline,
                timeout=25,
            )
            inspection_bytes = (json.dumps(raw_report, ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            inspection = raw_report
        else:
            inspection, inspection_bytes = read_json(args.inspection)
        report = inspect(inspection, inspection_bytes, baseline, baseline_bytes, matrix,
                         matrix_bytes, integration, integration_bytes, source=source)
        exit_code = 0 if args.allow_blocked else 2
    except (DiagnosticError, ValueError, TypeError, OSError, RuntimeError, KeyError) as error:
        # Deliberately do not echo file paths, raw stdout/stderr or provider text.
        report = {"format": "aik-upstream-diagnostic-v1", "task_id": "AIK-008",
                  "status": "FAIL", "code": "INVALID_DIAGNOSTIC_INPUT",
                  "qualification": "NOT_QUALIFIED"}
        exit_code = 1

    content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        except OSError:
            print(json.dumps({"status": "FAIL", "code": "OUTPUT_WRITE_FAILED"}))
            return 1
    else:
        print(content, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
