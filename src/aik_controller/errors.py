"""Controller errors carry fixed classifications, never secrets or raw VM text."""


class ControllerError(Exception):
    """The controller operation could not be completed."""


class InvalidControllerConfig(ControllerError, ValueError):
    """A controller argument, port or policy violates its contract."""
