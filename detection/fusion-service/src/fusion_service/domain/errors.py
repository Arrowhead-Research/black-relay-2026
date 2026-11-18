"""Exceptions that cross layer boundaries. Messages are reason codes, never input values."""


class Rejected(ValueError):
    """A permanent input error; the reason is safe to log without the source payload."""


class Unsupported(Rejected):
    """The topic or schema is not one this service knows how to normalize."""


class IdentityConflict(Rejected):
    """A source id was reused for a different payload inside the dedupe window."""


class CursorExpired(ValueError):
    """The requested cursor is below the retention floor; `floor` is where to resume."""

    def __init__(self, floor: int):
        super().__init__(f"cursor below floor {floor}")
        self.floor = floor


class InvalidCursor(ValueError):
    """The requested cursor is ahead of anything this database has assigned."""


class StorageUnavailable(RuntimeError):
    """Storage cannot accept or serve right now; callers retry later without acknowledging."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NodeUnreachable(RuntimeError):
    """A sensor node did not answer a request as expected; `reason` is a code, never a URL."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NodeTimeout(NodeUnreachable):
    """A sensor node accepted the connection but did not answer in time."""
