"""A2 (same-user) producer configuration: compose and check like VM _load_a2_producer.

The private key file is never opened by compose. check reads it only to verify
its shape; the privateKeyPkcs8 value is discarded immediately and never stored,
logged or returned. Key/pin pair agreement needs Ed25519 support outside the
standard library, so it is reported as NOT_CHECKED rather than assumed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os

from ._io import dumps_document, loads_strict, read_regular_file
from .canonical import a2_digest
from .contract import (
    A2_CONFIG_FORMAT, A2_HOST_ID, A2_MAX_FILE_BYTES, A2_PROFILE, A2_PROFILE_ID,
    A2_RESOURCE_KINDS, A2_SELECTION_SOURCE, PROTECTED_PRODUCER_PATHS, a2_profile_dict,
)
from .errors import GovernanceEnvError

CONFIG_JSON_INVALID = "A2_PRODUCER_CONFIG_JSON_INVALID"
PROFILE_MISMATCH = "A2_PRODUCER_PROFILE_MISMATCH"
NAMESPACE_MISMATCH = "A2_PRODUCER_NAMESPACE_MISMATCH"
RESOURCE_BINDING_MISMATCH = "A2_PRODUCER_RESOURCE_BINDING_MISMATCH"
FREEZE_IDENTITY_MISMATCH = "A2_PRODUCER_FREEZE_IDENTITY_MISMATCH"
KEY_UNAVAILABLE = "A2_PRODUCER_KEY_UNAVAILABLE"
KEY_MISMATCH = "A2_PRODUCER_KEY_MISMATCH"
PIN_UNAVAILABLE = "A2_PRODUCER_PIN_UNAVAILABLE"
PIN_MISMATCH = "A2_PRODUCER_PIN_MISMATCH"
PIN_SET_DIGEST_MISMATCH = "A2_PRODUCER_PIN_SET_DIGEST_MISMATCH"
PROTECTED_PRODUCER_PATH = "PROTECTED_PRODUCER_PATH"

ASSURANCE_NOTE = (
    "same-user: the signature shows only issuance by the selected producer and call binding; "
    "it is not evidence of observation truthfulness or OS isolation."
)

_TOP_KEYS = frozenset({"format", "selection", "resources"})
_SELECTION_KEYS = frozenset({"source", "profile", "pinSetDigest", "resourceBindingDigest", "freezeIdentity"})
_RESOURCE_KEYS = frozenset(A2_RESOURCE_KINDS)
_ENTRY_KEYS = frozenset({"namespace", "location"})
_KEY_FILE_KEYS = frozenset({"version", "namespace", "keyId", "hostId", "privateKeyPkcs8"})
_PIN_FILE_KEYS = frozenset({"namespace", "pins"})
_PIN_ENTRY_KEYS = frozenset({"keyId", "publicKeySpki", "status"})
_PROTECTED = frozenset(path.casefold() for path in PROTECTED_PRODUCER_PATHS)


@dataclass(frozen=True, slots=True)
class A2ConfigReport:
    profile_id: str
    freeze_identity: str
    key_id: str
    pin_set_digest: str
    assurance: str = "same-user"
    assurance_note: str = ASSURANCE_NOTE
    key_pin_match: str = "NOT_CHECKED"


def _path_invalid(kind: str) -> str:
    # Same code family as VM _read_a2_json: A2_PRODUCER_<KIND>_PATH_INVALID.
    return f"A2_PRODUCER_{kind.upper()}_PATH_INVALID"


def _location(value, kind: str) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise GovernanceEnvError(_path_invalid(kind), check=kind)
    if kind in ("key", "pin") and value.casefold() in _PROTECTED:
        raise GovernanceEnvError(PROTECTED_PRODUCER_PATH, check=kind)
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise GovernanceEnvError(_path_invalid(kind), check=kind)
    return value


def _is_base64(value) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError:
        return False
    nonempty = len(decoded) > 0
    del decoded
    return nonempty


def _read_pin(location: str) -> dict:
    data = read_regular_file(location, limit=A2_MAX_FILE_BYTES, code=PIN_UNAVAILABLE)
    try:
        document = loads_strict(data)
    except ValueError:
        raise GovernanceEnvError(PIN_MISMATCH) from None
    if type(document) is not dict:
        raise GovernanceEnvError(PIN_MISMATCH)
    return document


def _pin_digest(pin: dict) -> str:
    try:
        return a2_digest(pin)
    except GovernanceEnvError:
        raise GovernanceEnvError(PIN_MISMATCH) from None


def compose_a2_config(*, key_location: str, pin_location: str, state_location: str) -> bytes:
    locations = {
        "key": _location(key_location, "key"),
        "pin": _location(pin_location, "pin"),
        "state": _location(state_location, "state"),
    }
    pin_set_digest = _pin_digest(_read_pin(locations["pin"]))
    resources = {
        kind: {"namespace": A2_PROFILE[f"{kind}Namespace"], "location": locations[kind]}
        for kind in A2_RESOURCE_KINDS
    }
    try:
        binding = a2_digest(resources)
    except GovernanceEnvError:
        raise GovernanceEnvError("A2_PRODUCER_RESOURCE_BINDING_MISMATCH") from None
    selection = {
        "source": A2_SELECTION_SOURCE,
        "profile": a2_profile_dict(),
        "pinSetDigest": pin_set_digest,
        "resourceBindingDigest": binding,
    }
    selection["freezeIdentity"] = a2_digest(selection)
    return dumps_document({"format": A2_CONFIG_FORMAT, "selection": selection, "resources": resources})


def _check_key(location: str) -> str:
    data = read_regular_file(location, limit=A2_MAX_FILE_BYTES, code=KEY_UNAVAILABLE)
    try:
        document = loads_strict(data)
    except ValueError:
        document = None
    del data
    if type(document) is not dict:
        raise GovernanceEnvError(KEY_MISMATCH)
    try:
        if set(document) != _KEY_FILE_KEYS:
            raise GovernanceEnvError(KEY_MISMATCH)
        version = document["version"]
        if type(version) is not int or version != 1:
            raise GovernanceEnvError(KEY_MISMATCH)
        if document["namespace"] != A2_PROFILE["keyNamespace"] or document["hostId"] != A2_HOST_ID:
            raise GovernanceEnvError(KEY_MISMATCH)
        key_id = document["keyId"]
        if type(key_id) is not str or not key_id.strip():
            raise GovernanceEnvError(KEY_MISMATCH)
        # Shape only; the value itself is dropped with the document below.
        if not _is_base64(document["privateKeyPkcs8"]):
            raise GovernanceEnvError(KEY_MISMATCH)
        return key_id
    finally:
        document.clear()


def _check_pin(pin: dict, key_id: str) -> None:
    if set(pin) != _PIN_FILE_KEYS or pin["namespace"] != A2_PROFILE["pinNamespace"]:
        raise GovernanceEnvError(PIN_MISMATCH)
    pins = pin["pins"]
    if type(pins) is not list:
        raise GovernanceEnvError(PIN_MISMATCH)
    matches = [entry for entry in pins if type(entry) is dict and entry.get("keyId") == key_id]
    if len(matches) != 1:
        raise GovernanceEnvError(PIN_MISMATCH)
    entry = matches[0]
    if set(entry) != _PIN_ENTRY_KEYS or entry["status"] != "active" or not _is_base64(entry["publicKeySpki"]):
        raise GovernanceEnvError(PIN_MISMATCH)


def check_a2_config(raw: bytes) -> A2ConfigReport:
    try:
        config = loads_strict(raw)
    except ValueError:
        raise GovernanceEnvError(CONFIG_JSON_INVALID) from None
    if type(config) is not dict:
        raise GovernanceEnvError(CONFIG_JSON_INVALID)
    # VM checks the top-level keys and format together with the selection shape
    # and reports all of them as A2_PRODUCER_PROFILE_MISMATCH (vibemarshal 32bb0f9).
    if set(config) != _TOP_KEYS or config["format"] != A2_CONFIG_FORMAT:
        raise GovernanceEnvError(PROFILE_MISMATCH)

    selection = config["selection"]
    if type(selection) is not dict or set(selection) != _SELECTION_KEYS:
        raise GovernanceEnvError(PROFILE_MISMATCH)
    if selection["source"] != A2_SELECTION_SOURCE:
        raise GovernanceEnvError(PROFILE_MISMATCH)
    profile = selection["profile"]
    if type(profile) is not dict or profile != dict(A2_PROFILE):
        raise GovernanceEnvError(PROFILE_MISMATCH)

    resources = config["resources"]
    if type(resources) is not dict or set(resources) != _RESOURCE_KEYS:
        raise GovernanceEnvError(NAMESPACE_MISMATCH)
    for kind in A2_RESOURCE_KINDS:
        entry = resources[kind]
        if type(entry) is not dict or set(entry) != _ENTRY_KEYS:
            raise GovernanceEnvError(NAMESPACE_MISMATCH)
        if entry["namespace"] != A2_PROFILE[f"{kind}Namespace"]:
            raise GovernanceEnvError(NAMESPACE_MISMATCH)
    locations = {kind: _location(resources[kind]["location"], kind) for kind in A2_RESOURCE_KINDS}

    try:
        binding = a2_digest(resources)
    except GovernanceEnvError:
        raise GovernanceEnvError(RESOURCE_BINDING_MISMATCH) from None
    if selection["resourceBindingDigest"] != binding:
        raise GovernanceEnvError(RESOURCE_BINDING_MISMATCH)

    unfrozen = {key: value for key, value in selection.items() if key != "freezeIdentity"}
    try:
        freeze = a2_digest(unfrozen)
    except GovernanceEnvError:
        raise GovernanceEnvError(FREEZE_IDENTITY_MISMATCH) from None
    if selection["freezeIdentity"] != freeze:
        raise GovernanceEnvError(FREEZE_IDENTITY_MISMATCH)

    key_id = _check_key(locations["key"])
    pin = _read_pin(locations["pin"])
    _check_pin(pin, key_id)
    pin_set_digest = _pin_digest(pin)
    if selection["pinSetDigest"] != pin_set_digest:
        raise GovernanceEnvError(PIN_SET_DIGEST_MISMATCH)

    return A2ConfigReport(
        profile_id=A2_PROFILE_ID, freeze_identity=freeze, key_id=key_id, pin_set_digest=pin_set_digest,
    )
