"""Prometheus metrics in the text exposition format, with no new dependency.

``prometheus_client`` would be the obvious choice and is deliberately not used:
it brings a C-accelerated dependency into a package that also gets frozen with
PyInstaller for the desktop bundle, and it installs a multiprocess mode whose
correct configuration is a footgun under Gunicorn. What Osprey needs is four
counters, two gauges and a histogram, and the text format is trivial to emit.

Cardinality is bounded on purpose. Request metrics are labelled by method and
*route template* (``/projects/{project_id}/hotlist``), never by the resolved
path -- labelling by path would mint a new time series per project id and take
the scrape target down within a day.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

#: Seconds. Chosen around the latencies that matter here: a hotlist refresh runs
#: hundreds of milliseconds, a connector poll seconds, an export longer still.
DEFAULT_BUCKETS = (0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_LabelKey = tuple[tuple[str, str], ...]


def _key(labels: dict[str, str] | None) -> _LabelKey:
    return tuple(sorted((labels or {}).items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(key: _LabelKey, extra: tuple[str, str] | None = None) -> str:
    pairs = list(key) + ([extra] if extra else [])
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + inner + "}"


class _Metric:
    def __init__(self, name: str, help_text: str, kind: str) -> None:
        self.name = name
        self.help_text = help_text
        self.kind = kind
        self._lock = threading.Lock()


class Counter(_Metric):
    """Monotonically increasing total."""

    def __init__(self, name: str, help_text: str) -> None:
        super().__init__(name, help_text, "counter")
        self._values: dict[_LabelKey, float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._values[_key(labels)] += amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            snapshot = dict(self._values)
        for key, value in sorted(snapshot.items()):
            lines.append(f"{self.name}{_render_labels(key)} {value:g}")
        return lines


class Gauge(_Metric):
    """A value that goes up and down."""

    def __init__(self, name: str, help_text: str) -> None:
        super().__init__(name, help_text, "gauge")
        self._values: dict[_LabelKey, float] = defaultdict(float)

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._values[_key(labels)] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._values[_key(labels)] += amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        with self._lock:
            snapshot = dict(self._values)
        for key, value in sorted(snapshot.items()):
            lines.append(f"{self.name}{_render_labels(key)} {value:g}")
        return lines


class Histogram(_Metric):
    """Cumulative buckets plus sum and count, as Prometheus expects."""

    def __init__(
        self, name: str, help_text: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> None:
        super().__init__(name, help_text, "histogram")
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[_LabelKey, list[int]] = {}
        self._sums: dict[_LabelKey, float] = defaultdict(float)
        self._totals: dict[_LabelKey, int] = defaultdict(int)

    def observe(self, value: float, **labels: str) -> None:
        key = _key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, edge in enumerate(self.buckets):
                if value <= edge:
                    counts[index] += 1
            self._sums[key] += value
            self._totals[key] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        with self._lock:
            counts = {k: list(v) for k, v in self._counts.items()}
            sums = dict(self._sums)
            totals = dict(self._totals)
        for key in sorted(counts):
            cumulative = 0
            for index, edge in enumerate(self.buckets):
                # Buckets are cumulative: observe() already added to every bucket
                # whose edge the value fits under, so these counts are the totals.
                cumulative = counts[key][index]
                lines.append(
                    f"{self.name}_bucket"
                    f"{_render_labels(key, ('le', _format_edge(edge)))} {cumulative}"
                )
            lines.append(f"{self.name}_bucket{_render_labels(key, ('le', '+Inf'))} {totals[key]}")
            lines.append(f"{self.name}_sum{_render_labels(key)} {sums[key]:g}")
            lines.append(f"{self.name}_count{_render_labels(key)} {totals[key]}")
        return lines


def _format_edge(edge: float) -> str:
    return repr(edge) if edge != int(edge) else str(int(edge))


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
_REGISTRY: list[_Metric] = []


def _register(metric):
    _REGISTRY.append(metric)
    return metric


http_requests_total = _register(
    Counter("osprey_http_requests_total", "HTTP requests by method, route and status.")
)
http_request_duration_seconds = _register(
    Histogram("osprey_http_request_duration_seconds", "HTTP request latency in seconds.")
)
http_requests_in_flight = _register(
    Gauge("osprey_http_requests_in_flight", "HTTP requests currently being served.")
)
rate_limit_rejections_total = _register(
    Counter("osprey_rate_limit_rejections_total", "Requests refused by a rate limiter.")
)
auth_failures_total = _register(
    Counter("osprey_auth_failures_total", "Failed authentication attempts by reason.")
)
connector_polls_total = _register(
    Counter("osprey_connector_polls_total", "Connector poll cycles by source type and outcome.")
)
signals_ingested_total = _register(
    Counter("osprey_signals_ingested_total", "Signals persisted, by source type.")
)
items_scored_total = _register(Counter("osprey_items_scored_total", "Items scored, by bucket."))
connections_by_status = _register(Gauge("osprey_connections", "Configured connections by status."))
build_info = _register(Gauge("osprey_build_info", "Build metadata; the value is always 1."))
process_start_time_seconds = _register(
    Gauge("osprey_process_start_time_seconds", "Unix start time of this process.")
)

process_start_time_seconds.set(time.time())


def set_build_info(version: str, env: str) -> None:
    build_info.set(1, version=version, env=env)


def render() -> str:
    """The full exposition payload."""
    lines: list[str] = []
    for metric in _REGISTRY:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def reset() -> None:
    """Clear every series. Tests only -- a scrape target must never do this."""
    for metric in _REGISTRY:
        if isinstance(metric, Counter | Gauge):
            metric._values.clear()  # noqa: SLF001
        elif isinstance(metric, Histogram):
            metric._counts.clear()  # noqa: SLF001
            metric._sums.clear()  # noqa: SLF001
            metric._totals.clear()  # noqa: SLF001
