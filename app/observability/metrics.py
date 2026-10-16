import threading
from bisect import bisect_left

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)


def _key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(key: tuple[tuple[str, str], ...], extra: str = "") -> str:
    parts = [f'{k}="{_escape(v)}"' for k, v in key] + ([extra] if extra else [])
    return "{" + ",".join(parts) + "}" if parts else ""


class Counter:
    def __init__(self, name: str, help: str) -> None:
        self.name, self.help = name, help
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1, **labels: str) -> None:
        with self._lock:
            k = _key(labels)
            self._values[k] = self._values.get(k, 0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(_key(labels), 0)

    def total(self) -> float:
        return sum(self._values.values())

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            out += [
                f"{self.name}{_render_labels(k)} {v:g}" for k, v in sorted(self._values.items())
            ]
        return out


class Histogram:
    def __init__(self, name: str, help: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self.name, self.help, self.buckets = name, help, buckets
        self._data: dict[tuple, list[float]] = {}  # per label set: bucket counts, sum, count
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        with self._lock:
            row = self._data.setdefault(_key(labels), [0] * (len(self.buckets) + 2))
            row[bisect_left(self.buckets, value)] += 1
            row[-2] += value
            row[-1] += 1

    def count(self, **labels: str) -> float:
        row = self._data.get(_key(labels))
        return row[-1] if row else 0

    def reset(self) -> None:
        with self._lock:
            self._data.clear()

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            for k, row in sorted(self._data.items()):
                cumulative = 0
                for bound, n in zip(self.buckets, row, strict=False):
                    cumulative += n
                    out.append(
                        f"{self.name}_bucket{_render_labels(k, f'le="{bound:g}"')} {cumulative:g}"
                    )
                out.append(f"{self.name}_bucket{_render_labels(k, 'le="+Inf"')} {row[-1]:g}")
                out.append(f"{self.name}_sum{_render_labels(k)} {row[-2]:g}")
                out.append(f"{self.name}_count{_render_labels(k)} {row[-1]:g}")
        return out


class Registry:
    def __init__(self) -> None:
        self._metrics: list[Counter | Histogram] = []

    def counter(self, name: str, help: str) -> Counter:
        metric = Counter(name, help)
        self._metrics.append(metric)
        return metric

    def histogram(
        self, name: str, help: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> Histogram:
        metric = Histogram(name, help, buckets)
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        return "\n".join(line for m in self._metrics for line in m.render()) + "\n"

    def reset(self) -> None:
        for metric in self._metrics:
            metric.reset()


REGISTRY = Registry()

HTTP_REQUESTS = REGISTRY.counter(
    "http_requests_total", "HTTP requests by route template and status"
)
HTTP_LATENCY = REGISTRY.histogram("http_request_duration_seconds", "HTTP request latency")
TRANSITIONS = REGISTRY.counter("booking_transitions_total", "Booking state transitions")
OPERATIONS = REGISTRY.counter("provider_operations_total", "Provider operation lifecycle events")
OPERATION_LATENCY = REGISTRY.histogram(
    "provider_operation_latency_seconds", "Time from an operation being queued to its final result"
)
EVENTS = REGISTRY.counter("provider_events_total", "Provider events by type and outcome")
AUTH_FAILURES = REGISTRY.counter("auth_failures_total", "Rejected authentication attempts by kind")
SESSION_CONFLICTS = REGISTRY.counter("session_conflicts_total", "Lost compare-and-swap saves")
SPAN_LATENCY = REGISTRY.histogram("span_duration_seconds", "Timed internal steps")
AUDIT_EVENTS = REGISTRY.counter("audit_events_total", "Audit events by name")
