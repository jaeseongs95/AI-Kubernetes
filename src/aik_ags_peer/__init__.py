"""AGS 2.9.0 session-message (PEER) bridge for AIK orchestrator/worker sessions.

AGS is an independent product. This package uses only the public
``session-message-cli.mjs`` stdin/stdout contract. It does not import or copy
AGS modules or storage formats. The ``aik.peer.v1`` body envelope and the
``aik-worker:<id>`` address are AIK conventions, not part of the AGS contract.
Implementation and fake-runner tests do not qualify a real worker-host
deployment. Real environment checks are NOT_RUN unless explicitly executed.
"""

from .addresses import AddressBook, ags_peer_id, worker_address
from .bridge import PeerBridge
from .cli import AgsSessionCli, OPERATIONS, PROTOCOL_VERSION, STATE_DIR_ENV, RunResult
from .envelope import KINDS, MAX_BODY_BYTES, SCHEMA, Envelope, decode, encode
from .errors import AgsPeerError
from .journal import PeerJournal
from .models import (
    PEER_STATES, SEND_PHASES, ObserveResult, PeerEvent, ResultMessage, SendRecord, WorkerAddress,
)

__all__ = [
    "AddressBook", "ags_peer_id", "worker_address", "PeerBridge", "AgsSessionCli", "OPERATIONS",
    "PROTOCOL_VERSION", "STATE_DIR_ENV", "RunResult", "KINDS", "MAX_BODY_BYTES", "SCHEMA",
    "Envelope", "decode", "encode", "AgsPeerError", "PeerJournal", "PEER_STATES", "SEND_PHASES",
    "ObserveResult", "PeerEvent", "ResultMessage", "SendRecord", "WorkerAddress",
]
