"""The decision layer: constraint-aware allocation recommendations.

The brief's engineering loop is *Observe → Detect → Predict → Decide →
Simulate → Act → Monitor → Recover*. The intelligence layer (Day 3) covers
Observe, Detect and Predict. This package covers Decide, Simulate and Act
— and it puts a human between Decide and Act.

The modules:

``constraints``
    The simulator's eight allocation rules, in the order it applies them.
``world``
    A frozen snapshot of the network that the optimizer reasons over.
``validator``
    Local pre-validation, so the engine never proposes a 409.
``optimizer``
    The priority-greedy allocation policy.
``models``
    The recommendation and its lifecycle.
``service``
    Propose, simulate, approve, submit, reconcile.
"""

from .constraints import ConstraintCode, ConstraintReport, ConstraintViolation
from .models import (
    Alternative,
    Recommendation,
    RecommendationStatus,
    RiskProjection,
)
from .optimizer import AllocationOptimizer, OptimizerConfig
from .service import CONFLICT_EXPLANATIONS, DecisionError, DecisionService
from .validator import AllocationValidator
from .world import (
    AllocationSnapshot,
    DepotSnapshot,
    RouteSnapshot,
    StationSnapshot,
    SupplyArrivalSnapshot,
    WorldSnapshot,
)

__all__ = [
    "CONFLICT_EXPLANATIONS",
    "AllocationOptimizer",
    "AllocationSnapshot",
    "AllocationValidator",
    "Alternative",
    "ConstraintCode",
    "ConstraintReport",
    "ConstraintViolation",
    "DecisionError",
    "DecisionService",
    "DepotSnapshot",
    "OptimizerConfig",
    "Recommendation",
    "RecommendationStatus",
    "RiskProjection",
    "RouteSnapshot",
    "StationSnapshot",
    "SupplyArrivalSnapshot",
    "WorldSnapshot",
]
