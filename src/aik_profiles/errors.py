"""Profile errors carry classifications only, never supplied values."""

from aik_config.errors import InvalidSetting, SettingsError


class ProfileError(SettingsError):
    """An execution profile operation could not be completed."""


class InvalidProfile(InvalidSetting, ProfileError):
    """A profile, choice, approval or profile document violates its contract."""


class CapabilityMismatch(ProfileError):
    """The requested profile has no supported binding (fail-closed)."""

    def __init__(self, message: str, *, decision=None):
        super().__init__(message)
        self.decision = decision
