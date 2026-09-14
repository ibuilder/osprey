"""Connector plugins. Importing this package registers the built-in connectors.

Connectors shipped outside this repository are discovered through the
``osprey.connectors`` entry-point group, so adding a source never needs a core
edit (SPEC §6). A package declares, in its ``pyproject.toml``::

    [project.entry-points."osprey.connectors"]
    mysource = "osprey_connector_mysource"

Installing that package into the same environment as Osprey is enough: the module
is imported at startup, and its ``@registry.register`` decorator does the rest.
See ``connectors-sdk/`` for the guide and a template.
"""

import logging
from importlib.metadata import entry_points

# Import side effect: each module registers its connector(s) on the registry.
from . import (  # noqa: F401
    argus,
    filedrop,
    gcal,
    gmail,
    internal,
    outlook,
    procore,
)
from .base import Connection, Connector, Health, NormalizedSignal, RawEvent, registry

log = logging.getLogger("osprey.connectors")

PLUGIN_GROUP = "osprey.connectors"


def load_plugins() -> list[str]:
    """Import every installed connector plugin. Returns the names that loaded.

    A plugin that fails to import is logged and skipped rather than raised: one
    broken third-party package must not stop Osprey, or every other source, from
    starting. That includes a plugin that tries to reuse a built-in's
    ``source_type``, which the registry refuses.
    """
    loaded: list[str] = []
    for entry in entry_points(group=PLUGIN_GROUP):
        try:
            entry.load()
        except Exception:  # noqa: BLE001 - isolate each plugin
            log.exception("could not load connector plugin %r (%s)", entry.name, entry.value)
            continue
        loaded.append(entry.name)
        log.info("loaded connector plugin %r from %s", entry.name, entry.value)
    return loaded


load_plugins()

__all__ = [
    "PLUGIN_GROUP",
    "Connection",
    "Connector",
    "Health",
    "NormalizedSignal",
    "RawEvent",
    "load_plugins",
    "registry",
]
