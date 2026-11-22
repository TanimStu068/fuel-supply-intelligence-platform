"""The constraint model: every rule the simulator enforces, in one place.

The Integration Guide (§5.2) validates an allocation in a fixed order, and
the **first** failure wins. That order is not cosmetic — it decides which
reason an operator sees when a request is wrong in several ways at once.
The engine therefore applies the rules in exactly the documented order, so
a recommendation that passes local validation is a recommendation the
simulator should accept.

The eight rules the decision engine checks:

===========================  ==========================================
Code                         Rule
===========================  ==========================================
``ROUTE_MISMATCH``           the route does not connect the depot and
                             station named in the request
``DEPOT_CLOSED``             the depot is neither OPEN nor CONSTRAINED
``STATION_CLOSED``           the station is not OPEN
``ROUTE_DISRUPTED``          the route is not AVAILABLE
``ROUTE_CAPACITY_EXCEEDED``  quantity > route.max_shipment
``INSUFFICIENT_INVENTORY``   the depot does not hold that much fuel
``DISPATCH_CAPACITY_EXCEEDED``  the depot's per-tick dispatch allowance
                             would be exceeded
``DESTINATION_CAPACITY_EXCEEDED``  the station's tank would overflow
===========================  ==========================================

Pre-validating locally is not a micro-optimisation. Every avoidable 409 is
a request the simulator has to parse, log and reject, and — more
importantly — an allocation that *looks* approved but was refused. The
engine treats a constraint violation as a **reason not to propose**, not
as an error to recover from.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConstraintCode(StrEnum):
    """The simulator's allocation conflict codes (guide §5.2 / §9)."""

    ROUTE_MISMATCH = "ROUTE_MISMATCH"
    DEPOT_CLOSED = "DEPOT_CLOSED"
    STATION_CLOSED = "STATION_CLOSED"
    ROUTE_DISRUPTED = "ROUTE_DISRUPTED"
    ROUTE_CAPACITY_EXCEEDED = "ROUTE_CAPACITY_EXCEEDED"
    INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
    DISPATCH_CAPACITY_EXCEEDED = "DISPATCH_CAPACITY_EXCEEDED"
    DESTINATION_CAPACITY_EXCEEDED = "DESTINATION_CAPACITY_EXCEEDED"


#: The order the guide validates in. The first failure wins, so the engine
#: checks them in this order too — otherwise a proposal could be rejected
#: for a different reason than the one we predicted.
VALIDATION_ORDER: tuple[ConstraintCode, ...] = (
    ConstraintCode.ROUTE_MISMATCH,
    ConstraintCode.DEPOT_CLOSED,
    ConstraintCode.STATION_CLOSED,
    ConstraintCode.ROUTE_DISRUPTED,
    ConstraintCode.ROUTE_CAPACITY_EXCEEDED,
    ConstraintCode.INSUFFICIENT_INVENTORY,
    ConstraintCode.DISPATCH_CAPACITY_EXCEEDED,
    ConstraintCode.DESTINATION_CAPACITY_EXCEEDED,
)


@dataclass(slots=True)
class ConstraintCheck:
    """The outcome of one constraint check."""

    code: ConstraintCode
    #: True when the constraint is satisfied.
    satisfied: bool
    #: What was measured, and the limit it was measured against.
    detail: str
    #: The number the check compared, for the operator's benefit.
    observed: float | None = None
    #: The limit it was compared against.
    limit: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "satisfied": self.satisfied,
            "detail": self.detail,
            "observed": round(self.observed, 3) if self.observed is not None else None,
            "limit": round(self.limit, 3) if self.limit is not None else None,
        }


@dataclass(slots=True)
class ConstraintViolation:
    """A constraint that blocks this allocation."""

    code: ConstraintCode
    detail: str
    observed: float | None = None
    limit: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "detail": self.detail,
            "observed": round(self.observed, 3) if self.observed is not None else None,
            "limit": round(self.limit, 3) if self.limit is not None else None,
        }


@dataclass(slots=True)
class ConstraintReport:
    """Every check that was made, and which ones failed."""

    checks: list[ConstraintCheck]

    @property
    def violations(self) -> list[ConstraintViolation]:
        """The checks that failed, in the guide's validation order."""
        failed = {check.code: check for check in self.checks if not check.satisfied}
        return [
            ConstraintViolation(
                code=code,
                detail=failed[code].detail,
                observed=failed[code].observed,
                limit=failed[code].limit,
            )
            for code in VALIDATION_ORDER
            if code in failed
        ]

    @property
    def is_valid(self) -> bool:
        """True when nothing blocks the allocation."""
        return not self.violations

    @property
    def primary_violation(self) -> ConstraintViolation | None:
        """The violation the simulator would report — the first one."""
        violations = self.violations
        return violations[0] if violations else None

    def to_payload(self) -> list[dict[str, object]]:
        return [check.to_payload() for check in self.checks]
