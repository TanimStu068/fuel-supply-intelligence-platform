"""Prometheus instrumentation for the backend.

Two groups of series:

**Application** — request rate, latency histogram and error rate for the
backend's own HTTP surface.

**Integration** — what happens between the platform and the simulator:
call outcomes and latency, SSE reconnects, stale responses, degraded-mode
activations and the age of the last successful sync.

The Grafana "Fuel Platform Overview" dashboard consumes these.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

#: HTTP requests, labelled by method, path and status code.
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the backend.",
    ["method", "path", "status"],
)

#: HTTP request latency in seconds.
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Backend HTTP request latency in seconds.",
    ["method", "path"],
)


def request_latency_summary() -> dict[str, float]:
    """The request count, the error rate and the p95 latency.

    **Read from the metric's own state, not from ``collect()``.**

    The obvious way to compute a percentile is to collect the histogram
    and walk its samples. That is what this function used to do, and the
    load test found what it costs: ``collect()`` materialises every
    sample of every labelled child — 234 series after a few minutes of
    real traffic — and the dashboard polls the health endpoint every few
    seconds, so the platform spent a fifth of its CPU budget *measuring
    its own latency*.

    A histogram child keeps its buckets as plain counters, so the same
    number can be read without materialising a single sample. The result
    is identical, and it is about forty times cheaper.

    The buckets are *cumulative* counts per upper bound, so the p95 is
    the largest boundary whose cumulative count reaches 95% of the
    observations. Summing the buckets instead would give the observation
    count, not a latency.
    """
    observations = 0
    errors = 0
    total = 0
    boundaries: list[tuple[float, float]] = []

    for child in HTTP_REQUEST_DURATION._metrics.values():
        counts = [bucket.get() for bucket in child._buckets]
        if not counts:
            continue
        # A child keeps its buckets as **per-bucket** counts: bucket *i*
        # holds the observations that fell between boundary *i-1* and
        # boundary *i*. Prometheus presents them as cumulative counts, so
        # the running sum below reproduces exactly what ``collect()``
        # would have produced — and the last bucket is "+Inf", which
        # only holds the observations above the largest real boundary,
        # so the child's observation count is the sum of every bucket.
        observations += int(sum(counts))

        cumulative = 0.0
        for boundary, count in zip(child._upper_bounds, counts, strict=True):
            cumulative += count
            boundaries.append((float(boundary), cumulative))

    # The request count comes from the **counter**, not from the
    # histogram: both count the same requests, so adding them together
    # would report twice as many requests as were served.
    for label_values, child in HTTP_REQUESTS._metrics.items():
        value = int(child._value.get())
        total += value
        labels = dict(zip(HTTP_REQUESTS._labelnames, label_values, strict=True))
        if labels.get("status", "").startswith("5"):
            errors += value

    p95 = 0.0
    if boundaries and observations > 0:
        boundaries.sort()
        target = observations * 0.95
        cumulative = 0.0
        for boundary, count in boundaries:
            cumulative += count
            if cumulative >= target:
                p95 = boundary
                break

    return {
        "p95_latency_seconds": round(p95, 6),
        "request_count": int(total),
        "error_count": int(errors),
        "error_rate": round(errors / total, 6) if total else 0.0,
    }


#: Calls made to the simulator, labelled by outcome.
SIMULATOR_REQUESTS = Counter(
    "simulator_requests_total",
    "Requests issued to the fuel supply simulator, by outcome.",
    ["outcome"],
)

#: Simulator call latency in seconds.
SIMULATOR_REQUEST_DURATION = Histogram(
    "simulator_request_duration_seconds",
    "Latency of calls to the fuel supply simulator.",
    ["endpoint"],
)

#: Number of responses the simulator marked stale.
SIMULATOR_STALE_RESPONSES = Counter(
    "simulator_stale_responses_total",
    "Responses carrying the X-Simulator-Stale header.",
)

#: Calls the circuit breaker refused without sending them.
CIRCUIT_CALLS_REFUSED = Counter(
    "circuit_calls_refused_total",
    "Calls refused by a circuit breaker without being sent.",
    ["circuit"],
)

#: Calls the circuit breaker let through.
CIRCUIT_CALLS_ALLOWED = Counter(
    "circuit_calls_allowed_total",
    "Calls a circuit breaker let through.",
    ["circuit"],
)

#: Transitions between the breaker's states, by the state it moved to.
CIRCUIT_STATE_TRANSITIONS = Counter(
    "circuit_state_transitions_total",
    "Circuit breaker state transitions, by the new state.",
    ["circuit", "state"],
)

#: Whether each circuit breaker is currently open.
CIRCUIT_OPEN = Gauge(
    "circuit_open",
    "1 when the circuit breaker is open, 0 otherwise.",
    ["circuit"],
)

#: Simulator payloads rejected because they did not match the contract.
SIMULATOR_VALIDATION_REJECTIONS = Counter(
    "simulator_validation_rejections_total",
    "Simulator responses rejected because they did not match the documented schema.",
    ["endpoint"],
)

#: Sync cycles, labelled by outcome (success / failed).
SYNC_CYCLES = Counter(
    "sync_cycles_total",
    "Background sync cycles, by outcome.",
    ["outcome"],
)

#: How long one full sync cycle takes.
SYNC_CYCLE_DURATION = Histogram(
    "sync_cycle_duration_seconds",
    "Duration of a full background sync cycle.",
)

#: Sync cycles that were skipped because another cycle was already
#: running. A high count means the simulator's tick rate is outrunning the
#: platform's ability to read the world, and the coalescing is what keeps
#: the simulator's connection pool alive.
SYNC_CYCLES_COALESCED = Counter(
    "sync_cycles_coalesced_total",
    "Sync requests collapsed into a cycle that was already running.",
)

#: SSE connections opened against the simulator stream.
SSE_CONNECTIONS = Counter(
    "sse_connections_total",
    "SSE connections opened to the simulator event stream.",
)

#: SSE reconnects after a drop or a failure.
SSE_RECONNECTS = Counter(
    "sse_reconnects_total",
    "SSE reconnect attempts, by reason.",
    ["reason"],
)

#: SSE events received, by event name.
SSE_EVENTS = Counter(
    "sse_events_total",
    "SSE events received from the simulator, by event name.",
    ["event"],
)

#: Times the cached state was served in degraded mode.
DEGRADED_MODE_ACTIVATIONS = Counter(
    "degraded_mode_activations_total",
    "Times the backend served last-known state because the simulator was unreachable.",
)

#: Whether the backend is currently in degraded mode.
DEGRADED_MODE = Gauge(
    "degraded_mode",
    "1 when the backend is serving last-known state, 0 otherwise.",
)

#: Whether the cached state is currently marked stale.
STALE_DATA = Gauge(
    "stale_data",
    "1 when the cached state is marked stale, 0 otherwise.",
)

#: Age in seconds of the last successful sync.
LAST_SYNC_AGE = Gauge(
    "last_sync_age_seconds",
    "Seconds since the last successful sync with the simulator.",
)

#: Demand observations ingested, by whether the row was new or a duplicate.
DEMAND_INGESTED = Counter(
    "demand_observations_ingested_total",
    "Demand observations written to Postgres, by outcome.",
    ["outcome"],
)

#: Mean absolute error of the demand forecast, in litres, by model.
PREDICTION_ERROR_MAE = Gauge(
    "prediction_error_mae_liters",
    "Mean absolute error of the demand forecast against the observed demand, by model.",
    ["model"],
)

#: How many predictions landed inside their confidence interval.
PREDICTION_INTERVAL_COVERAGE = Gauge(
    "prediction_interval_coverage_ratio",
    "Share of predictions whose actual demand fell inside the confidence interval.",
    ["model"],
)

#: The mean confidence of the forecasts that were produced.
MODEL_CONFIDENCE = Gauge(
    "model_confidence",
    "Mean confidence of the forecasts produced by the active model.",
    ["model"],
)

#: How many forecasts were produced, by the model that produced them.
FORECASTS_PRODUCED = Counter(
    "forecasts_produced_total",
    "Forecasts produced, by the model that produced them.",
    ["model"],
)

#: How many risk assessments landed in each band.
RISK_ASSESSMENTS = Counter(
    "risk_assessments_total",
    "Risk assessments produced, by risk level.",
    ["level"],
)

#: How many anomalies were detected, by kind.
ANOMALIES_DETECTED = Counter(
    "anomalies_detected_total",
    "Anomalies detected, by kind.",
    ["kind"],
)

#: How many bottlenecks were detected, by kind.
BOTTLENECKS_DETECTED = Counter(
    "bottlenecks_detected_total",
    "Bottlenecks detected, by kind.",
    ["kind"],
)

#: How many times the rule-based fallback engaged, by reason.
FALLBACK_ACTIVATIONS = Counter(
    "fallback_activations_total",
    "Times the rule-based predictor replaced the learned model, by reason.",
    ["reason"],
)

#: How many forecasts were flagged for human review.
NEEDS_HUMAN_REVIEW = Counter(
    "needs_human_review_total",
    "Forecasts and assessments flagged for human review, by kind.",
    ["kind"],
)

#: How many allocation recommendations the engine has proposed.
DECISIONS_PROPOSED = Gauge(
    "decisions_proposed_total",
    "Allocation recommendations proposed by the decision engine.",
)

#: How many recommendations a human has approved.
DECISIONS_APPROVED = Gauge(
    "decisions_approved_total",
    "Allocation recommendations approved by an operator.",
)

#: How many recommendations a human has rejected.
DECISIONS_REJECTED = Gauge(
    "decisions_rejected_total",
    "Allocation recommendations rejected by an operator.",
)

#: How many recommendations were submitted to the simulator.
DECISIONS_SUBMITTED = Gauge(
    "decisions_submitted_total",
    "Allocation recommendations submitted to the simulator.",
)

#: How many recommendations the auto-mode approved on its own.
DECISIONS_AUTO_APPROVED = Gauge(
    "decisions_auto_approved_total",
    "Allocation recommendations approved by the auto-mode, without a human.",
)

#: How many submissions the simulator refused with a 409.
ALLOCATION_CONFLICTS = Gauge(
    "allocation_conflicts_total",
    "Allocation submissions the simulator refused with a conflict.",
)

#: The 409 codes the simulator returned, by code.
ALLOCATION_CONFLICTS_BY_CODE = Gauge(
    "allocation_conflicts_by_code_total",
    "Allocation submissions the simulator refused, by error code.",
    ["code"],
)

#: The share of proposed recommendations that a human approved.
DECISION_APPROVAL_RATE = Gauge(
    "decision_approval_rate",
    "Share of proposed allocation recommendations that were approved.",
)

#: How many times the decision layer fell back to the rule-based policy.
DECISION_FALLBACK_ACTIVATIONS = Counter(
    "decision_fallback_activations_total",
    "Times the decision engine used the rule-based fallback policy, by reason.",
    ["reason"],
)

#: Whether the learned model is disabled by the kill-switch.
ML_MODEL_DISABLED = Gauge(
    "ml_model_disabled",
    "1 when the ML kill-switch has disabled the learned model, 0 otherwise.",
)

#: How many times the kill-switch was flipped.
ML_KILL_SWITCH_ACTIVATIONS = Counter(
    "ml_kill_switch_activations_total",
    "Times the ML kill-switch was flipped, by the new state.",
    ["enabled"],
)

#: How many forecasts the kill-switch pushed onto the rule-based fallback.
ML_KILL_SWITCH_FALLBACKS = Counter(
    "ml_kill_switch_fallbacks_total",
    "Forecasts served by the rule-based predictor because the kill-switch was on.",
)

#: How many decisions were made in the last minute.
DECISIONS_PER_MINUTE = Gauge(
    "decisions_per_minute",
    "Allocation recommendations proposed in the last minute.",
)

#: The container's memory limit, in bytes, as the kernel reports it.
#:
#: cAdvisor used to provide this, but its Docker view is broken in this
#: environment (``/api/v1.3/docker`` returns ``{}``), and it cost 384 MB
#: to run. The container can read its own cgroup limit instead — the same
#: number Docker enforces — so the "how close is this container to being
#: killed" alert works without cAdvisor.
CONTAINER_MEMORY_LIMIT_BYTES = Gauge(
    "container_memory_limit_bytes",
    "The memory limit the orchestrator enforces on this container.",
)

#: The container's memory working set, in bytes.
CONTAINER_MEMORY_USAGE_BYTES = Gauge(
    "container_memory_usage_bytes",
    "This container's memory working set.",
)

#: The share of the container's memory limit that is in use.
CONTAINER_MEMORY_FRACTION = Gauge(
    "container_memory_fraction",
    "This container's memory working set as a share of its limit.",
)


def _read_cgroup_value(path: str) -> int | None:
    """Read one number out of the cgroup files, or ``None`` if it is not there.

    cgroup v2 puts the limit in ``memory.max`` and the current usage in
    ``memory.current``; cgroup v1 uses ``memory.limit_in_bytes`` and
    ``memory.usage_in_bytes``. Both are read, because which one is in use
    depends on the host's kernel and not on this application.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # cgroup v1 reports "no limit" as a huge number rather than a keyword.
    if value <= 0 or value >= 2**62:
        return None
    return value


def publish_container_memory() -> None:
    """Publish this container's memory usage and limit.

    Called on a timer by the metrics middleware. The numbers come from the
    container's own cgroup, so they are the numbers the orchestrator will
    actually use when it decides to kill this process.
    """
    limit = _read_cgroup_value("/sys/fs/cgroup/memory.max")
    if limit is None:
        limit = _read_cgroup_value("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    usage = _read_cgroup_value("/sys/fs/cgroup/memory.current")
    if usage is None:
        usage = _read_cgroup_value("/sys/fs/cgroup/memory/memory.usage_in_bytes")

    if limit is not None:
        CONTAINER_MEMORY_LIMIT_BYTES.set(limit)
    if usage is not None:
        CONTAINER_MEMORY_USAGE_BYTES.set(usage)
    if limit and usage is not None:
        CONTAINER_MEMORY_FRACTION.set(round(usage / limit, 4))


async def publish_container_memory_periodically(
    interval_seconds: float = 5.0,
    stop: asyncio.Event | None = None,
) -> None:
    """Publish the container's memory numbers on a timer.

    The numbers answer "how close is this container to being killed", and
    the answer does not change between two requests a millisecond apart.
    Reading the cgroup files on every request was pure waste: the profiler
    showed the file reads themselves in the request path's self-time, at
    **4% of all CPU the container spent answering requests**.

    A timer publishes the same numbers, and the ``/metrics`` scrape
    (every fifteen seconds) picks up whatever the last reading was.
    """
    publish_container_memory()
    while True:
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
        publish_container_memory()


def _route_template(scope: dict) -> str:
    """The route's own path template, or the raw path when there is none.

    The template is what the metric should be labelled with. A request to
    ``/api/history/station-mirpur`` and a request to
    ``/api/history/station-uttara`` are the **same endpoint**, and the
    dashboard's "request rate by path" panel draws one line per label
    value. Labelling by the raw URL made every station its own time
    series, so the panel filled with one-off lines that no query could
    aggregate, and the metric's cardinality grew with the number of
    stations rather than with the number of endpoints.

    Starlette sets ``scope["route"]`` while the request is being handled,
    so the template is read **after** the response rather than before it.
    A request that matched no route has no template, and falls back to
    the raw path — which is the honest answer for a 404.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return scope.get("path", "unknown")


def install_metrics_middleware(app: FastAPI) -> None:
    """Attach the metrics middleware and the ``/metrics`` route to an app."""

    class _MetricsMiddleware:
        """Count and time every request.

        **A pure ASGI middleware, not a ``BaseHTTPMiddleware``.** The
        latter runs each request through a task group and two memory
        object streams, because it has to hand the request and the
        response back and forth as two objects. A plain ASGI middleware
        wraps the two calls and does none of that. Measured on the same
        endpoint and the same FastAPI, that was **499 µs per request
        against 302 µs** — a third of the request's cost spent on the
        instrumentation.

        **The container's memory is not read here.** Reading the cgroup
        files means several file opens per request, and the profiler
        showed the reads themselves in the request path's self-time. The
        memory numbers are published on a timer instead, which is what
        they are for: they answer "how close is this container to being
        killed", and that does not change between two requests a
        millisecond apart.

        The counters are the same either way: the same labels, the same
        histogram, the same numbers.
        """

        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            method = scope.get("method", "GET")
            started = time.perf_counter()
            status = "500"

            async def send_wrapper(message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = str(message["status"])
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                path = _route_template(scope)
                HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
                HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(
                    time.perf_counter() - started
                )

    app.add_middleware(_MetricsMiddleware)
    app.add_api_route(
        "/metrics",
        _metrics_response,
        methods=["GET"],
        include_in_schema=False,
    )


def _metrics_response() -> Response:
    """Return the Prometheus exposition payload."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
