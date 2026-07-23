"""Connector plugins. Importing this package registers the built-in connectors."""

# Import side effect: each module registers its connector(s) on the registry.
from . import (  # noqa: F401
    filedrop,
    gcal,
    gmail,
    internal,
    outlook,
    procore,
)
from .base import Connection, Connector, Health, NormalizedSignal, RawEvent, registry

__all__ = [
    "Connection",
    "Connector",
    "Health",
    "NormalizedSignal",
    "RawEvent",
    "registry",
]
