"""Director errors carry classifications only, never secret or raw provider text."""


class DirectorError(Exception):
    """The director operation could not be completed."""


class InvalidDirectorConfig(DirectorError, ValueError):
    """The director configuration, runner or collaborator violates its contract."""


class InvalidRunnerResult(DirectorError, ValueError):
    """A runner returned a probe or session record outside the bounded contract."""
