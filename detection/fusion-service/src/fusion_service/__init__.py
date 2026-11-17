"""Black Relay central fusion REST service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("black-relay-event-fusion-api")
except PackageNotFoundError:  # running from an unpacked source tree
    __version__ = "0.0.0+unknown"
