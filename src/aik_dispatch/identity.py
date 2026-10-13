"""The exact identity of one dispatch: request, contract, Attempt, lease and connection.

Only observed provenance is stored. ``observed_permission`` and
``trusted_same_user`` stay None when nothing was observed; they are never
inferred from a model name, a host name or a default.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
import hashlib
import json
import ntpath
import posixpath
import re
from typing import Any, Mapping

from aik_provider.contracts import AuthKind, ConnectionBinding
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier, positive_integer


DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
DISPATCH_PATHS = ("native", "api")
_OBSERVED = re.compile(r"[\x20-\x7e]{1,256}\Z")
_MAX_CWD = 4096
_ID_FIELDS = ("request_id", "vm_project_id", "vm_task_id", "vm_attempt_id", "lease_id", "pool_id", "route_id")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def binding_fields(binding: ConnectionBinding) -> dict:
    return {field.name: plain(getattr(binding, field.name)) for field in fields(binding)}


def _absolute(path: str) -> bool:
    if posixpath.isabs(path):
        return True
    drive, rest = ntpath.splitdrive(path)
    return bool(drive) and rest.startswith(("\\", "/"))


@dataclass(frozen=True, slots=True)
class DispatchIdentity:
    request_id: str
    contract_digest: str
    vm_project_id: str
    vm_task_id: str
    vm_attempt_id: str
    lease_id: str
    fencing_token: int
    pool_id: str
    route_id: str
    binding: ConnectionBinding
    canonical_cwd: str
    input_digest: str
    path: str
    observed_permission: str | None = None
    trusted_same_user: bool | None = None

    def __post_init__(self) -> None:
        for name in _ID_FIELDS:
            identifier(getattr(self, name), name)
        positive_integer(self.fencing_token, "fencing_token")
        for name in ("contract_digest", "input_digest"):
            value = getattr(self, name)
            if type(value) is not str or DIGEST.fullmatch(value) is None:
                raise InvalidRecord(f"Invalid digest field: {name}.")
        if type(self.binding) is not ConnectionBinding:
            raise InvalidRecord("A dispatch requires an exact provider connection binding.")
        cwd = self.canonical_cwd
        if type(cwd) is not str or not 1 <= len(cwd) <= _MAX_CWD or "\x00" in cwd or not _absolute(cwd):
            raise InvalidRecord("A dispatch requires an absolute canonical working directory.")
        if type(self.path) is not str or self.path not in DISPATCH_PATHS:
            raise InvalidRecord("Unknown dispatch path.")
        if self.observed_permission is not None and (
            type(self.observed_permission) is not str or _OBSERVED.fullmatch(self.observed_permission) is None
        ):
            raise InvalidRecord("An observed permission must be a bounded printable description.")
        if self.trusted_same_user is not None and type(self.trusted_same_user) is not bool:
            raise InvalidRecord("trusted_same_user must be an observed boolean or None.")

    def to_dict(self) -> dict:
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        values["binding"] = binding_fields(self.binding)
        return values

    @property
    def identity_digest(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DispatchIdentity":
        if not isinstance(data, Mapping) or set(data) != {field.name for field in fields(cls)}:
            raise InvalidRecord("Invalid stored dispatch identity.")
        binding = data["binding"]
        if not isinstance(binding, Mapping) or set(binding) != {field.name for field in fields(ConnectionBinding)}:
            raise InvalidRecord("Invalid stored connection binding.")
        values = dict(binding)
        values["auth_kind"] = AuthKind(values["auth_kind"])
        return cls(**{**dict(data), "binding": ConnectionBinding(**values)})
