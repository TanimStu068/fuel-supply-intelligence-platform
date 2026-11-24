"""Async HTTP client for the BUP Fuel Supply Simulator.

Source of truth: docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf

Behaviour implemented here
-------------------------
* Every documented ``/v1`` GET endpoint, ``POST /v1/allocations``,
  ``POST /v1/allocations/{id}/cancel`` and the ``/admin`` control surface.
* Strict Pydantic validation of every response body; malformed payloads are
  rejected with :class:`SimulatorValidationError` and logged.
* Timeouts, plus retry with exponential backoff **and jitter** on
  ``503 FAULT_INJECTED`` (and on transport errors).
* Both error envelope shapes — ``{"error": {...}}`` and ``{"detail": {...}}`` —
  are parsed into the same typed exception hierarchy.
* The ``X-Simulator-Stale: true`` response header is surfaced as a boolean flag
  on every result and on the client itself.
* Both ``200`` and ``201`` are treated as success for idempotent replay of an
  allocation (guide §5.4 / §9).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from .circuit_breaker import CircuitBreaker
from .config import Settings, get_settings
from .metrics import (
    CIRCUIT_CALLS_ALLOWED,
    CIRCUIT_CALLS_REFUSED,
    SIMULATOR_REQUEST_DURATION,
    SIMULATOR_REQUESTS,
    SIMULATOR_STALE_RESPONSES,
    SIMULATOR_VALIDATION_REJECTIONS,
)
from .simulator_errors import (
    ALLOCATION_NOT_FOUND,
    CONFLICT_EXCEPTIONS,
    FAULT_INJECTED,
    NOT_FOUND,
    AllocationNotFoundError,
    ConflictError,
    EntityNotFoundError,
    FaultInjectedError,
    NotFoundError,
    SimulatorAPIError,
    SimulatorConnectionError,
    SimulatorTimeoutError,
    SimulatorValidationError,
    parse_error_detail,
)
from .simulator_models import (
    AdminActionResponse,
    AdminEvent,
    AdminFault,
    AdminStepResponse,
    Allocation,
    AllocationCreate,
    AuditLogEntry,
    DemandObservation,
    Depot,
    EventCreate,
    FaultCreate,
    FaultsClearedResponse,
    HealthResponse,
    Instance,
    Metrics,
    Region,
    Route,
    SimulatorEvent,
    Station,
    SupplyArrival,
)
from .sse import SSEParser, StreamEvent

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Header the simulator sets while a ``stale_data`` fault is active.
STALE_HEADER = "X-Simulator-Stale"

#: Status codes accepted as success for allocation writes (idempotent replay
#: answers 201 on a new allocation and may answer 200 on a replay).
SUCCESS_WRITE_CODES = frozenset({200, 201})


class SimulatorClient:
    """Async client for the fuel supply simulator.

    The client owns a single :class:`httpx.AsyncClient` so connections are
    pooled across calls. Use it as an async context manager, or let the
    FastAPI lifespan construct and close it.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.simulator_base_url.rstrip("/"),
            timeout=httpx.Timeout(
                self._settings.simulator_request_timeout,
                connect=self._settings.simulator_connect_timeout,
            ),
            headers={"Accept": "application/json"},
        )
        #: The breaker that protects the simulator from a retry storm.
        #: A retrying client against a pooled dependency is how the
        #: simulator's connection pool got exhausted in the first place.
        self._breaker = CircuitBreaker(
            name="simulator",
            failure_threshold=self._settings.circuit_breaker_failures,
            recovery_timeout=self._settings.circuit_breaker_recovery_seconds,
        )
        #: True when the simulator has served stale data since the last
        #: :meth:`reset_stale` call. Sticky on purpose: a snapshot built from
        #: several concurrent requests must report stale if *any* of its
        #: components were stale, and a per-response flag would be racy.
        self._stale = False
        #: Whether the single most recent response carried the stale header.
        self.last_stale = False
        #: Endpoint path of the most recent request, for error messages.
        self._last_path: str | None = None

    @property
    def breaker(self) -> CircuitBreaker:
        """The circuit breaker guarding the simulator."""
        return self._breaker

    @property
    def stale(self) -> bool:
        """True if any response since the last reset was served stale."""
        return self._stale

    def reset_stale(self) -> None:
        """Clear the sticky stale flag before starting a new snapshot."""
        self._stale = False
        self.last_stale = False

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client (only if this instance created it)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SimulatorClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- internals ---------------------------------------------------------

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _is_stale(response: httpx.Response) -> bool:
        """Detect the stale-data signal on a response."""
        return response.headers.get(STALE_HEADER, "").strip().lower() == "true"

    async def _sleep_backoff(self, attempt: int) -> None:
        """Sleep for an exponential backoff delay with full jitter.

        Full jitter (uniform in ``[0, base * 2**attempt]``) prevents a fleet of
        clients from re-hitting the simulator in lockstep after a fault.
        """
        base = self._settings.simulator_retry_base_delay
        ceiling = min(base * (2**attempt), self._settings.simulator_retry_max_delay)
        if ceiling <= 0:
            return
        await asyncio.sleep(random.uniform(0, ceiling))

    def _raise_for_error(self, response: httpx.Response, payload: Any) -> None:
        """Translate a non-success response into the typed exception hierarchy."""
        detail = parse_error_detail(payload)
        code = detail.code if detail else None
        message = detail.message if detail else f"HTTP {response.status_code}"

        if response.status_code == 409:
            exc_type = CONFLICT_EXCEPTIONS.get(code or "", ConflictError)
            raise exc_type(response.status_code, code, message, payload)
        if response.status_code == 404:
            if code == ALLOCATION_NOT_FOUND:
                exc_type: type[NotFoundError] = AllocationNotFoundError
            elif code == NOT_FOUND or code is None:
                exc_type = EntityNotFoundError
            else:
                exc_type = EntityNotFoundError
            raise exc_type(response.status_code, code, message, payload)
        if response.status_code == 503 and code == FAULT_INJECTED:
            raise FaultInjectedError(response.status_code, code, message, payload)
        raise SimulatorAPIError(response.status_code, code, message, payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        model: type[ModelT] | None = None,
        json_body: BaseModel | dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        success_codes: frozenset[int] | set[int] | None = None,
        max_retries: int | None = None,
    ) -> Any:
        """Perform a request with retry, validation and error translation.

        ``model`` selects the response schema; when given, the decoded body is
        validated strictly and a :class:`SimulatorValidationError` is raised on
        mismatch.
        """
        attempts_allowed = (
            self._settings.simulator_max_retries if max_retries is None else max_retries
        )
        payload = (
            json_body.model_dump(mode="json") if isinstance(json_body, BaseModel) else json_body
        )

        last_error: Exception | None = None
        for attempt in range(attempts_allowed + 1):
            self._last_path = path
            started = time.perf_counter()

            # The breaker is checked *before* the request, not after the
            # failure. A retrying client against a dependency with a small
            # connection pool is how that pool gets exhausted: every retry
            # takes a fresh connection while the previous one is still
            # waiting. Refusing the call outright gives the dependency room
            # to recover.
            if not self._breaker.allows():
                self._breaker.refuse()
                CIRCUIT_CALLS_REFUSED.labels(circuit="simulator").inc()
                raise SimulatorConnectionError(
                    f"the circuit breaker for the simulator is open; not calling {path}"
                )
            CIRCUIT_CALLS_ALLOWED.labels(circuit="simulator").inc()

            try:
                response = await self._client.request(
                    method,
                    path,
                    json=payload,
                    params=params,
                    headers=self._build_headers(headers),
                    timeout=self._timeout_for(path),
                )
            except httpx.TimeoutException as exc:
                last_error = SimulatorTimeoutError(f"timeout calling {path}: {exc}")
                self._breaker.record_failure()
                SIMULATOR_REQUESTS.labels(outcome="timeout").inc()
                logger.warning(
                    "simulator timeout",
                    extra={"path": path, "attempt": attempt + 1, "error": str(exc)},
                )
            except httpx.HTTPError as exc:
                last_error = SimulatorConnectionError(f"cannot reach simulator: {exc}")
                self._breaker.record_failure()
                SIMULATOR_REQUESTS.labels(outcome="unreachable").inc()
                logger.warning(
                    "simulator transport error",
                    extra={"path": path, "attempt": attempt + 1, "error": str(exc)},
                )
            else:
                SIMULATOR_REQUEST_DURATION.labels(endpoint=path).observe(
                    time.perf_counter() - started
                )
                self.last_stale = self._is_stale(response)
                if self.last_stale:
                    self._stale = True
                    SIMULATOR_STALE_RESPONSES.inc()
                    logger.warning(
                        "simulator served stale data",
                        extra={"path": path, "stale": True},
                    )

                ok_codes = success_codes or frozenset({200})
                if response.status_code in ok_codes:
                    SIMULATOR_REQUESTS.labels(outcome="success").inc()
                    decoded_value = self._decode(response, model, path)
                    self._breaker.record_success()
                    return decoded_value

                decoded: Any = None
                try:
                    decoded = response.json()
                except ValueError:
                    decoded = {"raw": response.text}

                try:
                    self._raise_for_error(response, decoded)
                except FaultInjectedError as exc:
                    last_error = exc
                    SIMULATOR_REQUESTS.labels(outcome="fault_injected").inc()
                    # An injected fault is the simulator deliberately
                    # misbehaving, not the dependency being broken, so it
                    # does not trip the breaker.
                    logger.warning(
                        "simulator fault injected",
                        extra={
                            "path": path,
                            "attempt": attempt + 1,
                            "code": exc.code,
                            "stale": self.last_stale,
                        },
                    )
                except ConflictError:
                    # A 409 is a *domain* answer: the simulator is healthy
                    # and the request conflicts with the current state. It
                    # must not trip the breaker, or a run of legitimate
                    # conflicts would make the platform think the
                    # simulator itself is broken.
                    SIMULATOR_REQUESTS.labels(outcome="conflict").inc()
                    raise
                except SimulatorAPIError:
                    SIMULATOR_REQUESTS.labels(outcome="error").inc()
                    self._breaker.record_failure()
                    raise
                else:  # pragma: no cover - _raise_for_error always raises
                    raise SimulatorAPIError(
                        response.status_code, None, "unexpected response", decoded
                    )

            if attempt < attempts_allowed:
                await self._sleep_backoff(attempt)

        assert last_error is not None
        raise last_error

    def _timeout_for(self, path: str) -> httpx.Timeout:
        """The timeout for one call to ``path``.

        The health probe gets a short one: it is on the container
        healthcheck's critical path, and a probe that waits out the full
        request timeout makes an unhealthy dependency look like a slow one.
        Every other call gets the configured request timeout.
        """
        if path.endswith("/health"):
            timeout = self._settings.simulator_health_timeout
        else:
            timeout = self._settings.simulator_request_timeout
        return httpx.Timeout(timeout, connect=self._settings.simulator_connect_timeout)

    def _decode(self, response: httpx.Response, model: type[ModelT] | None, path: str) -> Any:
        """Decode and (optionally) validate a successful response body."""
        try:
            data = response.json()
        except ValueError as exc:
            raise SimulatorValidationError(
                path, f"response body is not valid JSON: {exc}", response.text
            ) from exc

        if model is None:
            return data
        try:
            # ``model`` may be a concrete model or a ``list[Model]`` alias; the
            # TypeAdapter handles both uniformly.
            return TypeAdapter(model).validate_python(data)
        except ValidationError as exc:
            SIMULATOR_VALIDATION_REJECTIONS.labels(endpoint=path).inc()
            logger.error(
                "rejecting malformed simulator payload",
                extra={"path": path, "error": str(exc), "payload": data},
            )
            raise SimulatorValidationError(path, str(exc), data) from exc

    # -- health ------------------------------------------------------------

    async def health(self, *, fast: bool = False) -> HealthResponse:
        """GET /v1/health — liveness probe, bypasses fault injection.

        ``fast=True`` skips the retry loop, so a health probe fails fast
        instead of spending its whole budget backing off against a dependency
        that is down.
        """
        result = await self._request(
            "GET",
            "/v1/health",
            model=HealthResponse,
            max_retries=0 if fast else None,
        )
        assert isinstance(result, HealthResponse)
        return result

    # -- public /v1 reads --------------------------------------------------

    async def get_instance(self) -> Instance:
        """GET /v1/instance — current tick, sim_time, status, seed."""
        result = await self._request("GET", "/v1/instance", model=Instance)
        assert isinstance(result, Instance)
        return result

    async def get_regions(self) -> list[Region]:
        """GET /v1/regions."""
        result = await self._request("GET", "/v1/regions", model=list[Region])
        assert isinstance(result, list)
        return result

    async def get_depots(self) -> list[Depot]:
        """GET /v1/depots."""
        result = await self._request("GET", "/v1/depots", model=list[Depot])
        assert isinstance(result, list)
        return result

    async def get_depot(self, entity_id: str) -> Depot:
        """GET /v1/depots/{entity_id}."""
        result = await self._request("GET", f"/v1/depots/{entity_id}", model=Depot)
        assert isinstance(result, Depot)
        return result

    async def get_stations(self) -> list[Station]:
        """GET /v1/stations."""
        result = await self._request("GET", "/v1/stations", model=list[Station])
        assert isinstance(result, list)
        return result

    async def get_station(self, entity_id: str) -> Station:
        """GET /v1/stations/{entity_id}."""
        result = await self._request("GET", f"/v1/stations/{entity_id}", model=Station)
        assert isinstance(result, Station)
        return result

    async def get_routes(self) -> list[Route]:
        """GET /v1/routes."""
        result = await self._request("GET", "/v1/routes", model=list[Route])
        assert isinstance(result, list)
        return result

    async def get_supply_arrivals(self) -> list[SupplyArrival]:
        """GET /v1/supply-arrivals — sorted by planned_tick ascending."""
        result = await self._request("GET", "/v1/supply-arrivals", model=list[SupplyArrival])
        assert isinstance(result, list)
        return result

    async def get_events(self) -> list[SimulatorEvent]:
        """GET /v1/events — domain events, id-desc."""
        result = await self._request("GET", "/v1/events", model=list[SimulatorEvent])
        assert isinstance(result, list)
        return result

    async def get_allocations(self) -> list[Allocation]:
        """GET /v1/allocations — the shipment ledger, id-desc."""
        result = await self._request("GET", "/v1/allocations", model=list[Allocation])
        assert isinstance(result, list)
        return result

    async def get_demand_history(
        self,
        station_id: str | None = None,
        limit: int = 200,
    ) -> list[DemandObservation]:
        """GET /v1/demand-history — limit is clamped to [1, 2000] by the simulator."""
        params: dict[str, Any] = {"limit": limit}
        if station_id:
            params["station_id"] = station_id
        result = await self._request(
            "GET", "/v1/demand-history", params=params, model=list[DemandObservation]
        )
        assert isinstance(result, list)
        return result

    async def get_metrics(self) -> Metrics:
        """GET /v1/metrics — aggregated ground truth since simulation start."""
        result = await self._request("GET", "/v1/metrics", model=Metrics)
        assert isinstance(result, Metrics)
        return result

    # -- SSE stream --------------------------------------------------------

    async def stream(
        self,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Open ``GET /v1/stream`` and yield decoded events.

        The stream is a *hint* channel (guide §6): every event tells the
        caller that something changed, and the caller re-reads the affected
        resource over REST. There is no ``Last-Event-ID`` replay, so a
        reconnect loses whatever happened in between.

        The generator ends when the server closes the connection. A
        ``stream_disconnect`` fault makes the endpoint answer ``503``
        immediately, which surfaces as :class:`FaultInjectedError`.
        """
        timeout_config = httpx.Timeout(
            timeout if timeout is not None else self._settings.simulator_request_timeout,
            connect=self._settings.simulator_connect_timeout,
            read=None,
        )
        parser = SSEParser()

        try:
            async with self._client.stream(
                "GET",
                "/v1/stream",
                headers={"Accept": "text/event-stream"},
                timeout=timeout_config,
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {"raw": body.decode("utf-8", "replace")}
                    self._raise_for_error(response, payload)

                async for chunk in response.aiter_text():
                    for event in parser.feed(chunk):
                        yield event
        except httpx.TimeoutException as exc:
            raise SimulatorTimeoutError(f"timeout on SSE stream: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SimulatorConnectionError(f"SSE stream failed: {exc}") from exc

    # -- public /v1 writes -------------------------------------------------

    async def create_allocation(
        self,
        allocation: AllocationCreate | dict[str, Any],
    ) -> Allocation:
        """POST /v1/allocations — the only domain write.

        Both ``201`` (new allocation) and ``200`` (idempotent replay) are
        accepted and returned as an :class:`Allocation`.
        """
        body = (
            allocation
            if isinstance(allocation, AllocationCreate)
            else AllocationCreate.model_validate(allocation)
        )
        result = await self._request(
            "POST",
            "/v1/allocations",
            model=Allocation,
            json_body=body,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, Allocation)
        return result

    async def cancel_allocation(self, allocation_id: int) -> Allocation:
        """POST /v1/allocations/{allocation_id}/cancel — PENDING allocations only."""
        result = await self._request(
            "POST",
            f"/v1/allocations/{allocation_id}/cancel",
            model=Allocation,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, Allocation)
        return result

    # -- admin surface -----------------------------------------------------

    async def admin_run(self) -> AdminActionResponse:
        """POST /admin/run — start ticking."""
        result = await self._request(
            "POST", "/admin/run", model=AdminActionResponse, success_codes=SUCCESS_WRITE_CODES
        )
        assert isinstance(result, AdminActionResponse)
        return result

    async def admin_pause(self) -> AdminActionResponse:
        """POST /admin/pause — stop ticking."""
        result = await self._request(
            "POST",
            "/admin/pause",
            model=AdminActionResponse,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, AdminActionResponse)
        return result

    async def admin_toggle(self) -> AdminActionResponse:
        """POST /admin/toggle — flip RUNNING <-> PAUSED."""
        result = await self._request(
            "POST",
            "/admin/toggle",
            model=AdminActionResponse,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, AdminActionResponse)
        return result

    async def admin_step(self) -> AdminStepResponse:
        """POST /admin/step — advance exactly one deterministic tick."""
        result = await self._request(
            "POST",
            "/admin/step",
            model=AdminStepResponse,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, AdminStepResponse)
        return result

    async def admin_reset(self) -> AdminActionResponse:
        """POST /admin/reset — wipe the world and reload the baked scenario."""
        result = await self._request(
            "POST",
            "/admin/reset",
            model=AdminActionResponse,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, AdminActionResponse)
        return result

    async def admin_get_events(self) -> list[AdminEvent]:
        """GET /admin/events — last 50 rows, id-desc."""
        result = await self._request("GET", "/admin/events", model=list[AdminEvent])
        assert isinstance(result, list)
        return result

    async def admin_get_faults(self) -> list[AdminFault]:
        """GET /admin/faults — last 50 rows, id-desc."""
        result = await self._request("GET", "/admin/faults", model=list[AdminFault])
        assert isinstance(result, list)
        return result

    async def admin_inject_event(self, event: EventCreate | dict[str, Any]) -> AdminEvent:
        """POST /admin/events — inject a crisis event."""
        body = event if isinstance(event, EventCreate) else EventCreate.model_validate(event)
        result = await self._request(
            "POST",
            "/admin/events",
            model=AdminEvent,
            json_body=body,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, AdminEvent)
        return result

    async def admin_inject_fault(self, fault: FaultCreate | dict[str, Any]) -> AdminFault:
        """POST /admin/faults — inject a fault (auto-expiring).

        The simulator stores the fault as a wall-clock window and does **not**
        echo ``duration_seconds`` back, so the returned row exposes it through
        the derived :attr:`AdminFault.duration_seconds` property.
        """
        body = fault if isinstance(fault, FaultCreate) else FaultCreate.model_validate(fault)
        result = await self._request(
            "POST",
            "/admin/faults",
            model=AdminFault,
            json_body=body,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, AdminFault)
        return result

    async def admin_clear_faults(self) -> FaultsClearedResponse:
        """POST /admin/faults/clear — deactivate every fault (idempotent)."""
        result = await self._request(
            "POST",
            "/admin/faults/clear",
            model=FaultsClearedResponse,
            success_codes=SUCCESS_WRITE_CODES,
        )
        assert isinstance(result, FaultsClearedResponse)
        return result

    async def admin_audit(self, limit: int = 200) -> list[AuditLogEntry]:
        """GET /admin/audit — ground-truth audit log, limit clamped to [1, 1000]."""
        result = await self._request(
            "GET",
            "/admin/audit",
            params={"limit": limit},
            model=list[AuditLogEntry],
        )
        assert isinstance(result, list)
        return result


def build_client(settings: Settings | None = None) -> SimulatorClient:
    """Factory used by the FastAPI lifespan and by tests."""
    return SimulatorClient(settings=settings)
