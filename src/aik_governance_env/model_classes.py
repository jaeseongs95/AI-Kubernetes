"""Model class table: build from caller choices, check like VM load_model_classes.

A class is the caller's configured claim, not an observation. Names must be
provider-observed model names supplied by the caller (capability inventory).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib

from ._io import dumps_document, loads_strict
from .contract import MODEL_CLASSES, MODEL_CLASSES_FORMAT
from .errors import GovernanceEnvError


def build_model_class_table(assignments: Mapping[str, str], *, observed_models: Iterable[str]) -> bytes:
    if not isinstance(assignments, Mapping):
        raise GovernanceEnvError("INVALID_MODEL_CLASSES")
    if isinstance(observed_models, (str, bytes, bytearray)):
        raise GovernanceEnvError("INVALID_OBSERVED_MODELS")
    observed = frozenset(name for name in observed_models if type(name) is str)
    if not assignments:
        raise GovernanceEnvError("EMPTY_MODEL_CLASSES")
    classes: dict[str, str] = {}
    for name, model_class in assignments.items():
        if type(name) is not str or not name.strip():
            raise GovernanceEnvError("INVALID_MODEL_NAME")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise GovernanceEnvError("INVALID_MODEL_NAME") from None
        if type(model_class) is not str or model_class not in MODEL_CLASSES:
            raise GovernanceEnvError("INVALID_MODEL_CLASS")
        if name not in observed:
            raise GovernanceEnvError("MODEL_NOT_OBSERVED")
        classes[name] = model_class
    return dumps_document({"format": MODEL_CLASSES_FORMAT, "classes": classes})


def check_model_class_table(raw: bytes) -> tuple[dict, str]:
    def mismatch() -> GovernanceEnvError:
        return GovernanceEnvError("GOVERNANCE_CONTRACT_MISMATCH", check="model_class_table")

    try:
        document = loads_strict(raw)
    except ValueError:
        raise mismatch() from None
    if type(document) is not dict or set(document) != {"format", "classes"}:
        raise mismatch()
    if document["format"] != MODEL_CLASSES_FORMAT:
        raise mismatch()
    classes = document["classes"]
    if type(classes) is not dict or not classes:
        raise mismatch()
    for name, model_class in classes.items():
        if type(name) is not str or not name.strip():
            raise mismatch()
        if type(model_class) is not str or model_class not in MODEL_CLASSES:
            raise mismatch()
    return dict(classes), "sha256:" + hashlib.sha256(bytes(raw)).hexdigest()
