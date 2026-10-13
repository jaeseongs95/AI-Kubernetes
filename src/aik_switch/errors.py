"""Switch errors carry classifications only, never model output, directives or secrets."""


class SwitchError(Exception):
    """The switch operation could not be completed."""


class InvalidSwitch(SwitchError, ValueError):
    """A request, policy, event or argument violates the switch contract."""


class SwitchPending(SwitchError):
    """A switch is waiting for in-flight calls; new calls are not started meanwhile."""


class SwitchBlocked(SwitchError):
    """Re-authentication or a user decision is required before new calls."""


class SwitchConflict(SwitchError):
    """A different record already uses this event identity."""


class InvalidSwitchJournal(SwitchError):
    """The journal file is damaged, foreign, tampered or uses an unsupported schema."""
