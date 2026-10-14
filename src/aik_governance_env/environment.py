"""Combine the three governance inputs; expose env only when all are READY."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from ._io import read_regular_file
from .a2_profile import A2ConfigReport, check_a2_config
from .contract import ENV_A2_PROFILE, ENV_MODEL_CLASSES, ENV_PLUGIN_ROOT
from .errors import GovernanceEnvError
from .model_classes import check_model_class_table
from .plugin_root import PluginRootReport, inspect_plugin_root
from aik_platform.fs import sync_directory as _sync_directory

READY = "READY"
BLOCKED = "BLOCKED"
_INPUT_LIMIT = 1 << 20


@dataclass(frozen=True, slots=True)
class InputStatus:
    variable: str
    path: str
    state: str
    code: str | None = None
    check: str | None = None


@dataclass(frozen=True, slots=True)
class GovernanceEnvironment:
    statuses: tuple[InputStatus, ...]
    plugin_root: PluginRootReport | None
    model_class_digest: str | None
    model_class_names: tuple[str, ...]
    a2: A2ConfigReport | None

    @property
    def ready(self) -> bool:
        return bool(self.statuses) and all(status.state == READY for status in self.statuses)

    def env(self) -> dict[str, str]:
        """All three variables, or nothing: a partial environment is never returned."""
        if not self.ready:
            raise GovernanceEnvError("GOVERNANCE_ENVIRONMENT_BLOCKED")
        return {status.variable: status.path for status in self.statuses}


def _absolute(path) -> str:
    try:
        text = os.fspath(path)
    except TypeError:
        raise GovernanceEnvError("PATH_NOT_ABSOLUTE") from None
    if type(text) is not str or not os.path.isabs(text) or os.path.normpath(text) != text:
        raise GovernanceEnvError("PATH_NOT_ABSOLUTE")
    return text


def _display(path) -> str:
    try:
        return os.fspath(path) if isinstance(os.fspath(path), str) else ""
    except TypeError:
        return ""


def prepare_governance_environment(*, plugin_root, model_class_table_path, a2_config_path) -> GovernanceEnvironment:
    statuses: list[InputStatus] = []
    plugin_report = None
    digest = None
    names: tuple[str, ...] = ()
    a2_report = None

    try:
        location = _absolute(plugin_root)
        plugin_report = inspect_plugin_root(Path(location))
        statuses.append(InputStatus(ENV_PLUGIN_ROOT, location, READY))
    except GovernanceEnvError as error:
        statuses.append(InputStatus(ENV_PLUGIN_ROOT, _display(plugin_root), BLOCKED, error.code, error.check))

    try:
        location = _absolute(model_class_table_path)
        raw = read_regular_file(location, limit=_INPUT_LIMIT, code="INPUT_UNAVAILABLE", check="model_class_table")
        classes, digest = check_model_class_table(raw)
        names = tuple(sorted(classes))
        statuses.append(InputStatus(ENV_MODEL_CLASSES, location, READY))
    except GovernanceEnvError as error:
        digest = None
        statuses.append(InputStatus(ENV_MODEL_CLASSES, _display(model_class_table_path), BLOCKED,
                                    error.code, error.check))

    try:
        location = _absolute(a2_config_path)
        raw = read_regular_file(location, limit=_INPUT_LIMIT, code="INPUT_UNAVAILABLE", check="a2_config")
        a2_report = check_a2_config(raw)
        statuses.append(InputStatus(ENV_A2_PROFILE, location, READY))
    except GovernanceEnvError as error:
        statuses.append(InputStatus(ENV_A2_PROFILE, _display(a2_config_path), BLOCKED, error.code, error.check))

    return GovernanceEnvironment(tuple(statuses), plugin_report, digest, names, a2_report)


def write_atomic(path, data: bytes, *, exclusive: bool = True) -> Path:
    """Write via temp file + fsync + publish. exclusive=True never overwrites."""
    if not isinstance(data, (bytes, bytearray)):
        raise GovernanceEnvError("OUTPUT_INVALID")
    target = Path(_absolute_output(path))
    parent = target.parent
    if not parent.is_dir():
        raise GovernanceEnvError("OUTPUT_PATH_INVALID")
    if exclusive and (target.exists() or target.is_symlink()):
        raise GovernanceEnvError("OUTPUT_EXISTS")
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(bytes(data))
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, target)  # Atomic: fails if the target appeared.
            except FileExistsError:
                raise GovernanceEnvError("OUTPUT_EXISTS") from None
        else:
            os.replace(temporary, target)
        _sync_directory(parent)
    except OSError:
        raise GovernanceEnvError("OUTPUT_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
    return target


def _absolute_output(path) -> str:
    try:
        return _absolute(path)
    except GovernanceEnvError:
        raise GovernanceEnvError("OUTPUT_PATH_INVALID") from None
