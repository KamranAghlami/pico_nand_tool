"""Exception hierarchy. The CLI reports any NandToolError as `error: ...` instead of a traceback.

    NandToolError
    ├── FrameError        bytes arrived but don't form a valid frame
    ├── TransportError    the link failed (port error, timeout, retries exhausted)
    │   └── client.LostResponse   an erase/program got no valid answer: it may or may not have run
    ├── client.DeviceError        the device answered with a non-OK status
    │   └── client.WriteOpError   erase/program ran and the chip reported failure
    └── DumpError, ReconcileError, WriteError, ... (one per job module)

Anything else (a plain Python exception) is a bug, and the CLI lets it show a traceback.
"""


class NandToolError(Exception):
    """Base class for every error the tool reports to the user."""


class FrameError(NandToolError):
    """Received bytes do not form a valid response frame (bad magic, length, CRC or payload shape)."""


class TransportError(NandToolError):
    """The byte link failed: port error, timeout, or no valid response after all retries."""
