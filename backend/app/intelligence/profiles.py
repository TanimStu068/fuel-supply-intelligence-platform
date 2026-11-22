"""The simulator's demand model, expressed as data.

The guide (§8.5 and §8.6) publishes the demand profiles and the
hour-of-day factors. The simulator's own engine applies them like this::

    base   = daily_liters / 96          # 96 ticks per simulated day
    factor = hour_factor(hour, profile)
             * region.demand_factor
             * station.demand_multiplier
    demand = base * factor * jitter     # jitter = 1 +/- noise

The first two factors are **deterministic** — we know them exactly. Only the
jitter is random, and its magnitude is published as ``noise``.

That split is the whole basis of the intelligence layer: the deterministic
part is a strong baseline we can always compute, and the learned model only
has to explain what the baseline misses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FuelType = Literal["DIESEL", "PETROL", "OCTANE"]

#: The three fuel types the simulator models.
FUEL_TYPES: tuple[FuelType, ...] = ("DIESEL", "PETROL", "OCTANE")

#: Ticks per simulated day at the default 15-minute tick.
TICKS_PER_DAY = 96


@dataclass(frozen=True, slots=True)
class DemandProfile:
    """One demand profile: daily litres per fuel, plus the noise bound."""

    name: str
    daily_liters: dict[str, float]
    noise: float

    def base_per_tick(self, fuel: str) -> float:
        """The un-shaped litres of demand for one tick."""
        return self.daily_liters[fuel] / TICKS_PER_DAY


#: The four profiles the simulator ships (guide §8.5).
DEMAND_PROFILES: dict[str, DemandProfile] = {
    "urban_high": DemandProfile(
        name="urban_high",
        daily_liters={"DIESEL": 8500.0, "PETROL": 10500.0, "OCTANE": 5600.0},
        noise=0.10,
    ),
    "industrial": DemandProfile(
        name="industrial",
        daily_liters={"DIESEL": 14000.0, "PETROL": 4500.0, "OCTANE": 2200.0},
        noise=0.08,
    ),
    "highway": DemandProfile(
        name="highway",
        daily_liters={"DIESEL": 10500.0, "PETROL": 11000.0, "OCTANE": 6200.0},
        noise=0.12,
    ),
    "regional": DemandProfile(
        name="regional",
        daily_liters={"DIESEL": 7200.0, "PETROL": 7600.0, "OCTANE": 3600.0},
        noise=0.10,
    ),
}


def hour_factor(hour: int, profile: str) -> float:
    """The hour-of-day factor for a profile (guide §8.6).

    The busy windows are inclusive-start, exclusive-end, exactly as the
    simulator's engine applies them.
    """
    if profile == "industrial":
        return 1.55 if 6 <= hour < 18 else 0.45
    if profile == "highway":
        return 1.35 if (6 <= hour < 10 or 16 <= hour < 21) else 0.75
    if profile == "urban_high":
        return 1.45 if (7 <= hour < 10 or 16 <= hour < 21) else 0.70
    return 1.25 if 7 <= hour < 21 else 0.65


def get_profile(profile_name: str) -> DemandProfile:
    """Return a profile, falling back to ``regional`` for an unknown name.

    The simulator's engine does the same: anything it does not recognise
    falls through to the regional factors.
    """
    return DEMAND_PROFILES.get(profile_name, DEMAND_PROFILES["regional"])


def baseline_demand_per_tick(
    *,
    profile_name: str,
    fuel: str,
    hour: int,
    region_demand_factor: float,
    demand_multiplier: float,
) -> float:
    """The deterministic expected demand for one tick, in litres.

    This is the simulator's own formula without the random jitter — the part
    of the demand we can predict exactly.
    """
    profile = get_profile(profile_name)
    base = profile.base_per_tick(fuel)
    factor = hour_factor(hour, profile_name) * region_demand_factor * demand_multiplier
    return base * factor


def baseline_shape(
    *,
    profile_name: str,
    fuel: str,
    hour: int,
    region_demand_factor: float,
) -> float:
    """The demand shape without the station's own multiplier.

    Dividing an observation by this gives the station's *multiplier* — the
    factor that turns the published profile into what the station actually
    draws. That ratio is what the learned model predicts, because it is the
    same quantity for every station and every fuel, which lets one model
    serve the whole network.
    """
    profile = get_profile(profile_name)
    base = profile.base_per_tick(fuel)
    factor = hour_factor(hour, profile_name) * region_demand_factor
    return base * factor


def noise_bound(profile_name: str) -> float:
    """The published relative jitter for a profile.

    The simulator draws ``jitter = 1 + uniform(-noise, +noise)``, so the
    true demand always lands within ``+/- noise`` of the baseline.
    """
    return get_profile(profile_name).noise
