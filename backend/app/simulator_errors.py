"""Typed exceptions raised by the simulator client.

The guide documents two error envelope shapes (guide §9):

* domain errors — ``{"detail": {"code": "...", "message": "..."}}``
* injected faults — ``{"error": {"code": "FAULT_INJECTED", "message": "..."}}``

Both are parsed into the same :class:`SimulatorAPIError` base so callers never
need to know which envelope the simulator used. The 409 domain codes are mapped
to dedicated exception types (:class:`RouteMismatchError` and friends) so that
allocation policy code can branch on the *reason*, not on a string.
"""

from __future__ import annotations

from typing import Any

from .simulator_models import ErrorDetail

# --- 409 conflict codes (guide §5.2 / §9) --------------------------------

IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
ROUTE_MISMATCH = "ROUTE_MISMATCH"
DEPOT_CLOSED = "DEPOT_CLOSED"
STATION_CLOSED = "STATION_CLOSED"
ROUTE_DISRUPTED = "ROUTE_DISRUPTED"
ROUTE_CAPACITY_EXCEEDED = "ROUTE_CAPACITY_EXCEEDED"
INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
DISPATCH_CAPACITY_EXCEEDED = "DISPATCH_CAPACITY_EXCEEDED"
DESTINATION_CAPACITY_EXCEEDED = "DESTINATION_CAPACITY_EXCEEDED"
CANNOT_CANCEL = "CANNOT_CANCEL"

# --- 404 codes ------------------------------------------------------------

NOT_FOUND = "NOT_FOUND"
ALLOCATION_NOT_FOUND = "ALLOCATION_NOT_FOUND"

# --- 503 ------------------------------------------------------------------

FAULT_INJECTED = "FAULT_INJECTED"


class SimulatorError(Exception):
    """Base class for every error raised by the simulator client."""


class SimulatorConnectionError(SimulatorError):
    """The simulator could not be reached (DNS, refused, TLS, timeout)."""


class SimulatorTimeoutError(SimulatorConnectionError):
    """The simulator did not answer within the configured timeout."""


class SimulatorAPIError(SimulatorError):
    """Non-2xx response from the simulator.

    Attributes
    ----------
    status_code:
        HTTP status returned by the simulator.
    code:
        Machine-readable error code extracted from either envelope shape.
    message:
        Human-readable message extracted from either envelope shape.
    payload:
        The raw decoded body, kept for logging and debugging.
    """

    def __init__(
        self,
        status_code: int,
        code: str | None,
        message: str,
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.payload = payload
        super().__init__(f"HTTP {status_code} {code or 'UNKNOWN'}: {message}")

    @property
    def is_fault_injected(self) -> bool:
        """True when the simulator reported an injected fault (503)."""
        return self.code == FAULT_INJECTED


class SimulatorValidationError(SimulatorError):
    """The simulator returned a payload that does not match its documented schema.

    Raised instead of letting malformed data reach downstream state, per the
    brief's "Invalid simulator response -> Reject input + raise alert".
    """

    def __init__(self, endpoint: str, detail: str, payload: Any = None) -> None:
        self.endpoint = endpoint
        self.detail = detail
        self.payload = payload
        super().__init__(f"malformed response from {endpoint}: {detail}")


class ConflictError(SimulatorAPIError):
    """HTTP 409 — the request conflicts with current simulator state."""


class IdempotencyKeyMismatchError(ConflictError):
    """409 IDEMPOTENCY_KEY_MISMATCH — same key replayed with a different body."""


class RouteMismatchError(ConflictError):
    """409 ROUTE_MISMATCH — route does not connect the requested endpoints."""


class DepotClosedError(ConflictError):
    """409 DEPOT_CLOSED — depot status is neither OPEN nor CONSTRAINED."""


class StationClosedError(ConflictError):
    """409 STATION_CLOSED — station status is not OPEN."""


class RouteDisruptedError(ConflictError):
    """409 ROUTE_DISRUPTED — route status is not AVAILABLE."""


class RouteCapacityExceededError(ConflictError):
    """409 ROUTE_CAPACITY_EXCEEDED — quantity exceeds route max_shipment."""


class InsufficientInventoryError(ConflictError):
    """409 INSUFFICIENT_INVENTORY — depot does not hold enough fuel."""


class DispatchCapacityExceededError(ConflictError):
    """409 DISPATCH_CAPACITY_EXCEEDED — depot per-tick dispatch limit reached."""


class DestinationCapacityExceededError(ConflictError):
    """409 DESTINATION_CAPACITY_EXCEEDED — station capacity would be exceeded."""


class CannotCancelError(ConflictError):
    """409 CANNOT_CANCEL — allocation is no longer PENDING."""


class NotFoundError(SimulatorAPIError):
    """HTTP 404 — unknown depot, station, route, or allocation."""


class AllocationNotFoundError(NotFoundError):
    """404 ALLOCATION_NOT_FOUND — cancel on an unknown allocation id."""


class EntityNotFoundError(NotFoundError):
    """404 NOT_FOUND — unknown depot, station, or route id."""


class FaultInjectedError(SimulatorAPIError):
    """503 FAULT_INJECTED — an injected fault is active (retryable)."""


#: Maps a domain error code to the exception class raised for it.
CONFLICT_EXCEPTIONS: dict[str, type[ConflictError]] = {
    IDEMPOTENCY_KEY_MISMATCH: IdempotencyKeyMismatchError,
    ROUTE_MISMATCH: RouteMismatchError,
    DEPOT_CLOSED: DepotClosedError,
    STATION_CLOSED: StationClosedError,
    ROUTE_DISRUPTED: RouteDisruptedError,
    ROUTE_CAPACITY_EXCEEDED: RouteCapacityExceededError,
    INSUFFICIENT_INVENTORY: InsufficientInventoryError,
    DISPATCH_CAPACITY_EXCEEDED: DispatchCapacityExceededError,
    DESTINATION_CAPACITY_EXCEEDED: DestinationCapacityExceededError,
    CANNOT_CANCEL: CannotCancelError,
}


def parse_error_detail(payload: Any) -> ErrorDetail | None:
    """Extract the inner error object from either documented envelope shape.

    Returns ``None`` when the body does not contain a recognisable error
    object (for example FastAPI's ``{"detail": [...]}`` validation list).
    """
    if not isinstance(payload, dict):
        return None

    for envelope_key in ("error", "detail"):
        inner = payload.get(envelope_key)
        if isinstance(inner, dict) and "code" in inner:
            raw_code = inner.get("code")
            raw_message = inner.get("message", "")
            return ErrorDetail(
                code=str(raw_code),
                message=str(raw_message) if raw_message is not None else "",
            )
    return None
