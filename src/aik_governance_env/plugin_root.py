"""AGS plugin root structure check, reproducing VM GovernanceGate.preflight.

Digests here are local_derived; this module never starts the MCP server.
check_node() is a separate optional probe of ``node --version``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess

from ._io import loads_strict, read_regular_file
from .contract import MANIFEST, MANIFEST_FORMAT, MINIMUM_NODE, REQUIRED_ENTRY_POINTS
from .errors import GovernanceEnvError

_MANIFEST_LIMIT = 4 << 20
_NODE_VERSION = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,6})\Z")


@dataclass(frozen=True, slots=True)
class PluginRootReport:
    root: str
    manifest_sha256: str
    closure_tree_digest: str
    file_count: int
    entry_points: tuple[str, ...]
    provenance: str = "local_derived"


@dataclass(frozen=True, slots=True)
class NodeReport:
    state: str
    code: str | None
    version: tuple[int, int, int] | None
    minimum: tuple[int, int] = MINIMUM_NODE


def _mismatch(check: str) -> GovernanceEnvError:
    return GovernanceEnvError("GOVERNANCE_CONTRACT_MISMATCH", check=check)


def _inside(root: Path, relative) -> Path | None:
    if type(relative) is not str or not relative:
        return None
    try:
        candidate = (root / relative).resolve(strict=True)
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_plugin_root(root) -> PluginRootReport:
    try:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise _mismatch("manifest")
    except (OSError, RuntimeError, TypeError):
        raise _mismatch("manifest") from None
    manifest_path = resolved / MANIFEST
    raw = read_regular_file(manifest_path, limit=_MANIFEST_LIMIT,
                            code="GOVERNANCE_CONTRACT_MISMATCH", check="manifest")
    try:
        manifest = loads_strict(raw)
    except ValueError:
        raise _mismatch("manifest") from None
    if type(manifest) is not dict or manifest.get("format") != MANIFEST_FORMAT:
        raise _mismatch("manifest_format")
    entries = manifest.get("entryPoints")
    if type(entries) is not list:
        raise _mismatch("manifest_format")
    by_id: dict[str, dict] = {}
    for entry in entries:
        if (type(entry) is not dict or type(entry.get("id")) is not str or type(entry.get("path")) is not str
                or type(entry.get("executionClosure")) is not list
                or not all(type(item) is str for item in entry["executionClosure"])):
            raise _mismatch("manifest_format")
        if entry["id"] in by_id:
            raise _mismatch("manifest_format")
        by_id[entry["id"]] = entry

    files: dict[str, str] = {}
    try:
        for entry_id in REQUIRED_ENTRY_POINTS:
            entry = by_id.get(entry_id)
            if entry is None:
                raise _mismatch(f"entry_point:{entry_id}")
            target = _inside(resolved, entry["path"])
            if target is None:
                raise _mismatch(f"entry_point:{entry_id}")
            # Keyed by the manifest strings, as VM GovernanceGate.preflight does (vibemarshal 32bb0f9).
            files[entry["path"]] = _file_sha256(target)
            for item in entry["executionClosure"]:
                closure = _inside(resolved, item)
                if closure is None:
                    raise _mismatch("closure_file")
                files[item] = _file_sha256(closure)
    except OSError:
        raise _mismatch("closure_file") from None
    tree = "".join(f"{path}\0{sha}\n" for path, sha in sorted(files.items()))
    return PluginRootReport(
        root=str(resolved),
        manifest_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        closure_tree_digest="sha256:" + hashlib.sha256(tree.encode("utf-8")).hexdigest(),
        file_count=len(files),
        entry_points=REQUIRED_ENTRY_POINTS,
    )


def check_node(node_path, *, timeout: float = 10.0, runner=subprocess.run) -> NodeReport:
    """Optional probe: ``node --version`` must report at least MINIMUM_NODE."""
    try:
        completed = runner([str(node_path), "--version"], capture_output=True, text=True,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return NodeReport("BLOCKED", "NODE_TIMEOUT", None)
    except (OSError, ValueError):
        return NodeReport("BLOCKED", "NODE_UNAVAILABLE", None)
    if completed.returncode != 0:
        return NodeReport("BLOCKED", "NODE_UNAVAILABLE", None)
    match = _NODE_VERSION.fullmatch(str(completed.stdout or "").strip())
    if match is None:
        return NodeReport("BLOCKED", "NODE_VERSION_UNREADABLE", None)
    version = (int(match[1]), int(match[2]), int(match[3]))
    if version[:2] < MINIMUM_NODE:
        return NodeReport("BLOCKED", "NODE_VERSION_UNSUPPORTED", version)
    return NodeReport("READY", None, version)
