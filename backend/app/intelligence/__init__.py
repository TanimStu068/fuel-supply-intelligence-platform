"""The intelligence layer: forecasting, risk, anomaly and bottleneck detection.

The layer answers one question for an operator: **which station is about to
run dry, and why?**

Three parts:

``profiles``     the simulator's demand model (guide §8.5 and §8.6) as data
``forecasting``  two predictors and the one that picks between them
``risk``         hours-to-stockout, stockout probability and risk levels
``detection``    demand anomalies, inventory anomalies and bottlenecks

Everything here is deterministic and explainable: every score carries the
signals that produced it, so an operator can see *why* a station is red
rather than being handed a number.
"""

from __future__ import annotations

__all__ = [
    "detection",
    "forecasting",
    "profiles",
    "risk",
]
