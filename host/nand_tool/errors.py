"""Exception hierarchy. The CLI reports any NandToolError as `error: ...` instead of a traceback."""


class NandToolError(Exception):
    """Base class for every error the tool reports to the user."""


class FrameError(NandToolError):
    """Received bytes do not form a valid response frame (bad magic, length, CRC or payload shape)."""


class TransportError(NandToolError):
    """The byte link failed: port error, timeout, or no valid response after all retries."""
