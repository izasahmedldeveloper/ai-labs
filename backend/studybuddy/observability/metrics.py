"""Metrics registry (LLD 11 s3): counters, gauges, histograms, Prometheus text output and windowed queries.

Dependency-free stand-in for prometheus_client. Besides totals it keeps a bounded list of timestamped samples so
alert rules can ask "how many in the last 5 minutes" or "P95 over the last 10 minutes" without a Prometheus server.
"""
from __future__ import annotations

import math
import threading
import time
from bisect import bisect_left
from collections import deque
from typing import Callable

DEFAULT_BUCKETS = (50, 100, 250, 500, 1000, 1500, 2500, 4000, 6000, 10000, 30000)
_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:")


def _check_name(name: str) -> None:
    if not name or name[0].isdigit() or not set(name) <= _NAME_OK:
        raise ValueError(f"bad metric name: {name!r}")


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(pairs: tuple[tuple[str, str], ...]) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in pairs) + "}" if pairs else ""


def _fmt_num(x: float) -> str:
    if math.isinf(x):
        return "+Inf" if x > 0 else "-Inf"
    return str(int(x)) if float(x).is_integer() else repr(float(x))


class _Metric:
    kind = ""

    def __init__(self, registry: "Registry", name: str, help: str, labels: tuple[str, ...]):
        _check_name(name)
        for l in labels:
            _check_name(l)
        self.registry, self.name, self.help, self.labels = registry, name, help, tuple(labels)
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=registry.max_samples)  # (ts, key, value)

    def _key(self, labels: dict) -> tuple[tuple[str, str], ...]:
        if set(labels) != set(self.labels):
            raise ValueError(f"{self.name}: labels must be exactly {self.labels}, got {sorted(labels)}")
        return tuple((l, str(labels[l])) for l in self.labels)

    @staticmethod
    def _match(key, match: dict) -> bool:
        d = dict(key)
        return all(d.get(k) == str(v) for k, v in match.items())

    def _recent(self, seconds: float):
        cutoff = self.registry.clock() - seconds
        return [s for s in list(self._samples) if s[0] >= cutoff]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a):
        super().__init__(*a)
        self._totals: dict = {}

    def inc(self, n: float = 1, **labels) -> None:
        if n < 0:
            raise ValueError("a counter only goes up")
        key = self._key(labels)
        with self._lock:
            self._totals[key] = self._totals.get(key, 0) + n
            self._samples.append((self.registry.clock(), key, n))

    def value(self, **match) -> float:
        with self._lock:
            return sum(v for k, v in self._totals.items() if self._match(k, match))

    def window(self, seconds: float, **match) -> float:
        return sum(v for _, k, v in self._recent(seconds) if self._match(k, match))

    def series(self):
        with self._lock:
            return [(k, v) for k, v in sorted(self._totals.items())]


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, *a):
        super().__init__(*a)
        self._values: dict = {}

    def set(self, value: float, **labels) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def value(self, **match) -> float:
        with self._lock:
            vals = [v for k, v in self._values.items() if self._match(k, match)]
        return sum(vals)

    def series(self):
        with self._lock:
            return sorted(self._values.items())


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, registry, name, help, labels, buckets=DEFAULT_BUCKETS):
        super().__init__(registry, name, help, labels)
        self.buckets = tuple(sorted(buckets))
        self._data: dict = {}  # key -> [bucket counts..., +Inf count, sum]

    def observe(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            row = self._data.setdefault(key, [0] * (len(self.buckets) + 1) + [0.0])
            row[bisect_left(self.buckets, value)] += 1
            row[-1] += value
            self._samples.append((self.registry.clock(), key, float(value)))

    def count(self, **match) -> int:
        with self._lock:
            return sum(sum(r[:-1]) for k, r in self._data.items() if self._match(k, match))

    def quantile(self, q: float, seconds: float | None = None, **match) -> float | None:
        """Exact quantile of the retained samples (None if there are none). Window in seconds, or all retained."""
        src = self._recent(seconds) if seconds is not None else list(self._samples)
        vals = sorted(v for _, k, v in src if self._match(k, match))
        if not vals:
            return None
        return vals[min(len(vals) - 1, max(0, math.ceil(q * len(vals)) - 1))]

    def series(self):
        with self._lock:
            return [(k, list(r)) for k, r in sorted(self._data.items())]


class Registry:
    def __init__(self, clock: Callable[[], float] = time.time, max_samples: int = 20000):
        self.clock, self.max_samples = clock, max_samples
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _get(self, cls, name, help, labels, **kw):
        with self._lock:
            m = self._metrics.get(name)
            if m is not None:
                if type(m) is not cls or m.labels != tuple(labels):
                    raise ValueError(f"{name} already registered with a different type or labels")
                return m
            m = cls(self, name, help, tuple(labels), **kw)
            self._metrics[name] = m
            return m

    def counter(self, name, help="", labels=()) -> Counter:
        return self._get(Counter, name, help, labels)

    def gauge(self, name, help="", labels=()) -> Gauge:
        return self._get(Gauge, name, help, labels)

    def histogram(self, name, help="", labels=(), buckets=DEFAULT_BUCKETS) -> Histogram:
        return self._get(Histogram, name, help, labels, buckets=buckets)

    def get(self, name: str) -> _Metric:
        return self._metrics[name]

    def render(self, tenant_id: str | None = None) -> str:
        """Prometheus text format. With `tenant_id`, only series labelled with that tenant (the tenant view, LLD 11 s10):
        metrics without a tenant label are platform-wide and are left out."""
        out: list[str] = []
        for name in sorted(self._metrics):
            m = self._metrics[name]
            if tenant_id is not None and "tenant" not in m.labels:
                continue
            keep = lambda key: tenant_id is None or dict(key).get("tenant") == tenant_id
            lines: list[str] = []
            if isinstance(m, Histogram):
                for key, row in m.series():
                    if not keep(key):
                        continue
                    acc = 0
                    for b, c in zip(list(m.buckets) + [math.inf], row[:-1]):
                        acc += c
                        lines.append(f"{name}_bucket{_fmt_labels(key + (('le', _fmt_num(b)),))} {acc}")
                    lines.append(f"{name}_sum{_fmt_labels(key)} {_fmt_num(row[-1])}")
                    lines.append(f"{name}_count{_fmt_labels(key)} {acc}")
            else:
                lines = [f"{name}{_fmt_labels(k)} {_fmt_num(v)}" for k, v in m.series() if keep(k)]
            if lines:
                out += [f"# HELP {name} {m.help}", f"# TYPE {name} {m.kind}"] + lines
        return "\n".join(out) + ("\n" if out else "")
