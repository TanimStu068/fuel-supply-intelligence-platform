"""Forecasting: a deterministic baseline and a learned model, plus the
logic that picks between them.

**The shape is known exactly.** The simulator's demand is

    demand(t) = shape(t) * multiplier(t) * jitter(t)

where ``shape(t)`` is the published profile scaled by the hour-of-day factor
and the region factor, ``multiplier(t)`` is the station's demand multiplier
(a number the simulator publishes), and ``jitter(t)`` is bounded random
noise.

So the interesting part is not the shape — it is published — but the
**multiplier over the horizon**. A ``demand_spike`` event raises the
multiplier mid-horizon, and a forecast that assumes it stays flat is wrong
for every tick after the event starts.

That is what the learned model is for.

**The baseline** holds the multiplier flat across the horizon. It needs no
training data and never fails, so it is the floor the platform stands on.

**The learned model** predicts the multiplier's trajectory from the stored
history, and multiplies it by the known shape. One model serves the whole
network, because the multiplier is the same quantity for every station and
every fuel.

**The selector** chooses between them per forecast and reports which one was
used. It falls back to the baseline when the learned model is missing, has
not been trained, fails, or is not confident enough to trust.

Every forecast carries a confidence interval. The interval is not decorative:
when the learned model's interval is wide, the forecast is flagged for human
review.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .profiles import FUEL_TYPES, baseline_shape, get_profile, noise_bound

logger = logging.getLogger(__name__)


class ModelKind(StrEnum):
    """Which predictor produced a forecast."""

    #: The deterministic profile-based predictor.
    BASELINE = "baseline"
    #: The learned gradient-boosting model.
    LEARNED = "learned"


@dataclass(slots=True)
class ForecastPoint:
    """One forecast for one tick ahead."""

    tick: int
    sim_time: datetime
    predicted_demand: float
    lower_bound: float
    upper_bound: float
    confidence: float

    @property
    def width(self) -> float:
        """The width of the interval — how uncertain the point is."""
        return self.upper_bound - self.lower_bound


@dataclass(slots=True)
class Forecast:
    """A forecast for one (station, fuel) pair over the next N ticks."""

    station_id: str
    fuel_type: str
    model: ModelKind
    horizon_ticks: int
    points: list[ForecastPoint]
    #: 0..1 — how much to trust this forecast.
    confidence: float
    #: True when a human should look at this before it is acted on.
    needs_human_review: bool
    #: Why the fallback engaged, when it did.
    fallback_reason: str | None = None
    #: Free-form explanation of what drove the forecast.
    explanation: str = ""

    @property
    def total_demand(self) -> float:
        """The summed forecast over the horizon, in litres."""
        return sum(point.predicted_demand for point in self.points)

    @property
    def mean_demand_per_tick(self) -> float:
        """The average forecast per tick, in litres."""
        if not self.points:
            return 0.0
        return self.total_demand / len(self.points)

    @property
    def lower_total(self) -> float:
        """The summed lower bound over the horizon."""
        return sum(point.lower_bound for point in self.points)

    @property
    def upper_total(self) -> float:
        """The summed upper bound over the horizon."""
        return sum(point.upper_bound for point in self.points)

    def to_payload(self) -> dict[str, object]:
        """Render the forecast as a JSON-safe dictionary."""
        return {
            "station_id": self.station_id,
            "fuel_type": self.fuel_type,
            "model": self.model.value,
            "horizon_ticks": self.horizon_ticks,
            "confidence": round(self.confidence, 4),
            "needs_human_review": self.needs_human_review,
            "fallback_reason": self.fallback_reason,
            "explanation": self.explanation,
            "total_demand": round(self.total_demand, 3),
            "mean_demand_per_tick": round(self.mean_demand_per_tick, 3),
            "lower_total": round(self.lower_total, 3),
            "upper_total": round(self.upper_total, 3),
            "points": [
                {
                    "tick": point.tick,
                    "sim_time": point.sim_time.isoformat(),
                    "predicted_demand": round(point.predicted_demand, 3),
                    "lower_bound": round(point.lower_bound, 3),
                    "upper_bound": round(point.upper_bound, 3),
                    "confidence": round(point.confidence, 4),
                }
                for point in self.points
            ],
        }


@dataclass(slots=True)
class HistoryPoint:
    """One stored demand observation, as the forecaster sees it."""

    station_id: str
    fuel_type: str
    tick: int
    sim_time: datetime
    demand_liters: float
    served_liters: float
    unmet_liters: float


@dataclass(slots=True)
class StationContext:
    """Everything the forecaster needs to know about one station."""

    station_id: str
    profile_name: str
    region_demand_factor: float
    demand_multiplier: float
    current_tick: int
    tick_minutes: int = 15
    current_sim_time: datetime | None = None

    def hour_for_tick(self, tick: int) -> int:
        """The simulated hour of a future tick.

        The minutes carry: a tick at 06:45 plus one 15-minute tick is 07:00,
        which is a different demand shape than 06:00.
        """
        if self.current_sim_time is None:
            return 0
        return self.sim_time_for_tick(tick).hour

    def sim_time_for_tick(self, tick: int) -> datetime:
        """The simulated time of a future tick."""
        if self.current_sim_time is None:
            return datetime.now().astimezone()
        minutes_ahead = (tick - self.current_tick) * self.tick_minutes
        return self.current_sim_time + timedelta(minutes=minutes_ahead)

    def shape_for_tick(self, tick: int, fuel: str) -> float:
        """The known demand shape for a tick, without the multiplier."""
        return baseline_shape(
            profile_name=self.profile_name,
            fuel=fuel,
            hour=self.hour_for_tick(tick),
            region_demand_factor=self.region_demand_factor,
        )


def effective_multiplier(demand_liters: float, shape: float) -> float:
    """The multiplier that explains an observation.

    Dividing the observed demand by the known shape leaves the multiplier
    and the jitter. The jitter is bounded by the profile's published noise,
    so the ratio is a good estimate of the multiplier.
    """
    if shape <= 0:
        return 0.0
    return demand_liters / shape


# --- the baseline predictor ------------------------------------------------


class BaselinePredictor:
    """The deterministic predictor built from the published demand model.

    It holds the station's demand multiplier flat across the horizon and
    shapes it with the known profile. Its interval comes from the profile's
    published noise bound.

    The multiplier it holds flat is the **recently observed** one when
    there is history, and the simulator's declared one otherwise. That
    makes the fallback a *persistence* forecast: it needs no training, it
    never fails, and it still follows a spike that has already started,
    because a spike is visible in the observations.
    """

    #: How many recent observations the reference multiplier averages over.
    REFERENCE_WINDOW = 12

    def __init__(self, *, interval_scale: float = 1.0) -> None:
        self._interval_scale = interval_scale

    @property
    def name(self) -> str:
        return "profile-baseline"

    def forecast(
        self,
        *,
        context: StationContext,
        fuel: str,
        horizon_ticks: int,
        reference_multiplier: float | None = None,
    ) -> Forecast:
        """Forecast the next ``horizon_ticks`` ticks for one station and fuel.

        ``reference_multiplier`` overrides the station's declared multiplier.
        """
        multiplier = (
            context.demand_multiplier if reference_multiplier is None else reference_multiplier
        )
        source = (
            "the simulator's declared demand multiplier"
            if reference_multiplier is None
            else f"the last {self.REFERENCE_WINDOW} observed multipliers"
        )

        points: list[ForecastPoint] = []
        bound = noise_bound(context.profile_name) * self._interval_scale

        for offset in range(1, horizon_ticks + 1):
            tick = context.current_tick + offset
            shape = context.shape_for_tick(tick, fuel)
            expected = shape * multiplier
            points.append(
                ForecastPoint(
                    tick=tick,
                    sim_time=context.sim_time_for_tick(tick),
                    predicted_demand=expected,
                    lower_bound=max(0.0, expected * (1.0 - bound)),
                    upper_bound=expected * (1.0 + bound),
                    # The baseline is certain about the structure but not
                    # about the jitter, so its confidence is bounded by how
                    # much of the interval is noise.
                    confidence=round(max(0.0, min(1.0, 1.0 - bound)), 4),
                )
            )

        return Forecast(
            station_id=context.station_id,
            fuel_type=fuel,
            model=ModelKind.BASELINE,
            horizon_ticks=horizon_ticks,
            points=points,
            confidence=round(max(0.0, min(1.0, 1.0 - bound)), 4),
            needs_human_review=False,
            explanation=(
                f"Profile {context.profile_name} shaped by the hour-of-day factor and "
                f"the region factor ({context.region_demand_factor}), with the demand "
                f"multiplier held at {multiplier:.3f} from {source}."
            ),
        )

    def reference_multiplier(
        self,
        *,
        context: StationContext,
        fuel: str,
        history: list[HistoryPoint],
    ) -> float | None:
        """The recent observed multiplier for a station and fuel.

        The median is used rather than the mean: one spike tick should not
        drag the whole reference up.

        The window is the most **recent** observations, ordered by tick.
        Sorting by value instead would pick the largest multipliers of the
        whole history — the opposite of "recent" — and would bias every
        forecast upwards.
        """
        multipliers = _historical_multipliers(history, context, fuel)
        if not multipliers:
            return None

        ordered = sorted(multipliers.items())
        window = [value for _, value in ordered[-self.REFERENCE_WINDOW :]]
        middle = len(window) // 2
        if len(window) % 2 == 1:
            return window[middle]
        return (window[middle - 1] + window[middle]) / 2


# --- the learned model -----------------------------------------------------


def _baseline_multiplier(multipliers: dict[int, float] | None, target_tick: int) -> float:
    """What the rule-based baseline would predict for one tick.

    The baseline holds the station's *recently observed* multiplier flat, so
    on the training rows it predicts the median of the last three observed
    multipliers. This is what makes the two models comparable: the same
    prediction rule, evaluated on exactly the same rows.
    """
    if not multipliers:
        return 1.0
    recent = [
        multipliers[tick] for tick in range(target_tick - 3, target_tick) if tick in multipliers
    ]
    if not recent:
        return 1.0
    return float(statistics.median(recent))


def build_features(
    history: list[HistoryPoint],
    context: StationContext,
    fuel: str,
    target_tick: int,
    *,
    lag_values: list[float] | None = None,
    multipliers: dict[int, float] | None = None,
) -> list[float] | None:
    """Build the feature vector for one (station, fuel, tick) target.

    The target is the *multiplier*, so the features are the multiplier's
    own history plus the calendar and the station's static attributes.

    ``lag_values`` overrides the lags, which is what the recursive
    multi-step forecast uses for the ticks beyond the first.

    ``multipliers`` supplies a pre-computed multiplier history. Training
    builds one per (station, fuel) and reuses it, which turns a quadratic
    scan of the history into a linear one.
    """
    if lag_values is not None:
        lag_1, lag_2, lag_3 = lag_values
        mean_recent = sum(lag_values) / len(lag_values)
        trend = lag_1 - lag_2
    else:
        if multipliers is None:
            multipliers = _historical_multipliers(history, context, fuel)
        if multipliers is None:
            return None

        lag_1 = multipliers.get(target_tick - 1)
        lag_2 = multipliers.get(target_tick - 2)
        lag_3 = multipliers.get(target_tick - 3)
        if lag_1 is None or lag_2 is None:
            return None
        # The third lag may be missing at the start of a series; the most
        # recent value is a better stand-in than a hole in the features.
        if lag_3 is None:
            lag_3 = lag_2

        recent = [
            multipliers[tick] for tick in range(target_tick - 4, target_tick) if tick in multipliers
        ]
        if not recent:
            return None
        mean_recent = sum(recent) / len(recent)
        trend = lag_1 - lag_2

    hour = context.hour_for_tick(target_tick)
    sin_hour = math.sin(2 * math.pi * hour / 24)
    cos_hour = math.cos(2 * math.pi * hour / 24)

    return [
        float(target_tick),
        float(hour),
        sin_hour,
        cos_hour,
        lag_1,
        lag_2,
        lag_3,
        mean_recent,
        trend,
        context.demand_multiplier,
        context.region_demand_factor,
        _profile_index(context.profile_name),
    ]


def _historical_multipliers(
    history: list[HistoryPoint], context: StationContext, fuel: str
) -> dict[int, float] | None:
    """The observed multiplier at each historical tick."""
    relevant = [
        point
        for point in history
        if point.fuel_type == fuel and point.station_id == context.station_id
    ]
    if len(relevant) < 4:
        return None

    return {
        point.tick: effective_multiplier(
            point.demand_liters, _historical_shape(context, fuel, point)
        )
        for point in relevant
    }


def _historical_shape(context: StationContext, fuel: str, point: HistoryPoint) -> float:
    """The known shape at a historical tick.

    The hour comes from the observation's own simulated time, which is
    absolute and so cannot drift across a day boundary.
    """
    return baseline_shape(
        profile_name=context.profile_name,
        fuel=fuel,
        hour=point.sim_time.hour,
        region_demand_factor=context.region_demand_factor,
    )


#: The feature names, in the order :func:`build_features` produces them.
FEATURE_NAMES: tuple[str, ...] = (
    "target_tick",
    "hour",
    "hour_sin",
    "hour_cos",
    "multiplier_lag_1",
    "multiplier_lag_2",
    "multiplier_lag_3",
    "multiplier_mean_recent_4",
    "multiplier_trend",
    "demand_multiplier",
    "region_demand_factor",
    "profile_index",
)


def _profile_index(profile_name: str) -> float:
    """Encode the demand profile as a number.

    The four profiles have very different daily volumes, so the model needs
    to know which one it is looking at.
    """
    order = {"urban_high": 0.0, "industrial": 1.0, "highway": 2.0, "regional": 3.0}
    return order.get(profile_name, 3.0)


class LearnedModelUnavailable(RuntimeError):
    """The learned model could not be used.

    Raised when scikit-learn is not installed, the model is untrained, or
    training failed. The caller catches it and falls back to the baseline.
    """


@dataclass(slots=True)
class LearnedModel:
    """A trained gradient-boosting regressor plus its training metadata."""

    estimator: object
    #: Mean absolute error on the held-out split, in multiplier units.
    validation_mae: float
    #: Number of training rows.
    training_rows: int
    #: When it was trained.
    trained_at: datetime
    #: The training MAE, for comparison.
    training_mae: float = 0.0
    #: Extra metrics for the evaluation report.
    metadata: dict[str, float] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "gradient-boosting"


class HorizonValidator:
    """Scores a model the way it is actually used: over the horizon.

    A one-step-ahead error is the wrong yardstick here. The platform
    forecasts a whole day ahead and the forecast is *recursive* — the
    prediction for tick T+1 feeds the prediction for tick T+2 — so a
    small bias compounds with every step. A model that is excellent at one
    step and drifts badly over 96 steps is worse than useless for
    deciding whether a station will run dry tomorrow.

    The validator replays the recursive forecast over the held-out tail of
    the history and compares the *total* demand it predicts against the
    total that actually happened. Totalling is what matters operationally:
    the risk engine needs the sum of the demand over the horizon, not
    per-tick accuracy.
    """

    #: How many ticks ahead the recursive forecast is replayed.
    HORIZON_TICKS = 96

    def __init__(self, horizon_ticks: int | None = None) -> None:
        self.horizon_ticks = horizon_ticks or self.HORIZON_TICKS

    def score(
        self,
        predictor: LearnedPredictor,
        history: list[HistoryPoint],
        contexts: dict[str, StationContext],
    ) -> tuple[float, float] | None:
        """Return ``(model_total_mae, baseline_total_mae)`` in litres.

        ``None`` when there is not enough history to replay a horizon.
        """
        from .profiles import baseline_shape

        pairs: dict[tuple[str, str], list[HistoryPoint]] = {}
        for point in history:
            pairs.setdefault((point.station_id, point.fuel_type), []).append(point)

        model_errors: list[float] = []
        baseline_errors: list[float] = []

        for (station_id, fuel), points in pairs.items():
            context = contexts.get(station_id)
            if context is None or len(points) < self.horizon_ticks + 4:
                continue

            points = sorted(points, key=lambda p: p.tick)
            # Replay the last horizon's worth of ticks, using only the
            # history that precedes each one.
            origin = points[-self.horizon_ticks - 1]
            replay = points[-self.horizon_ticks :]

            actual_total = sum(point.demand_liters for point in replay)

            try:
                forecast = predictor.forecast(
                    context=context,
                    fuel=fuel,
                    horizon_ticks=self.horizon_ticks,
                    history=[p for p in points if p.tick <= origin.tick],
                )
            except Exception:
                continue

            model_total = sum(point.predicted_demand for point in forecast.points)

            # The baseline's total over the same window: the published
            # shape for each tick, with the observed multiplier held flat.
            #
            # The multiplier must come from the history *before* the
            # window — the same information the model was given. Taking it
            # from the end of the window hands the baseline the answer it
            # is being asked to predict, and the comparison is meaningless.
            multiplier = 1.0
            before_origin = [p for p in points if p.tick <= origin.tick]
            recent = before_origin[-12:]
            if recent:
                shape_samples = [
                    baseline_shape(
                        profile_name=context.profile_name,
                        fuel=fuel,
                        hour=point.sim_time.hour,
                        region_demand_factor=context.region_demand_factor,
                    )
                    for point in recent
                ]
                usable = [
                    point.demand_liters / shape
                    for point, shape in zip(recent, shape_samples, strict=True)
                    if shape > 0
                ]
                if usable:
                    multiplier = sum(usable) / len(usable)

            baseline_total = 0.0
            for point in replay:
                shape = baseline_shape(
                    profile_name=context.profile_name,
                    fuel=fuel,
                    hour=point.sim_time.hour,
                    region_demand_factor=context.region_demand_factor,
                )
                baseline_total += shape * multiplier

            model_errors.append(abs(model_total - actual_total))
            baseline_errors.append(abs(baseline_total - actual_total))

        if not model_errors:
            return None

        return sum(model_errors) / len(model_errors), sum(baseline_errors) / len(baseline_errors)


class LearnedPredictor:
    """Wraps the trained model and turns rows into forecasts.

    The import of scikit-learn happens inside :meth:`train` and
    :meth:`_predict_rows`, so the module imports cleanly without it and the
    platform falls back to the baseline.
    """

    def __init__(self) -> None:
        self._model: LearnedModel | None = None
        #: Scores the model over a whole forecast horizon, the way it is
        #: actually used.
        self._horizon_validator = HorizonValidator()

    @property
    def is_trained(self) -> bool:
        """True when a model is ready to predict."""
        return self._model is not None

    @property
    def model(self) -> LearnedModel | None:
        """The trained model, if any."""
        return self._model

    def train(
        self,
        history: list[HistoryPoint],
        contexts: dict[str, StationContext],
    ) -> LearnedModel:
        """Train on the stored history and validate on a held-out split.

        The split is *by time*, not random: the model is asked to predict
        ticks it has not seen, which is what it will be asked to do live.
        """
        try:
            from sklearn.ensemble import GradientBoostingRegressor
            from sklearn.metrics import mean_absolute_error
        except ImportError as exc:
            raise LearnedModelUnavailable(
                "scikit-learn is not installed; using the rule-based fallback"
            ) from exc

        rows: list[list[float]] = []
        targets: list[float] = []
        # The baseline's prediction for the same rows, so the two models can
        # be compared on exactly the same validation split.
        baseline_predictions: list[float] = []
        # The multiplier history is computed once per (station, fuel) and
        # reused for every row of that pair. Recomputing it per row made
        # training quadratic in the size of the history.
        multiplier_cache: dict[tuple[str, str], dict[int, float] | None] = {}

        for point in history:
            context = contexts.get(point.station_id)
            if context is None:
                continue

            key = (point.station_id, point.fuel_type)
            if key not in multiplier_cache:
                multiplier_cache[key] = _historical_multipliers(history, context, point.fuel_type)
            multipliers = multiplier_cache[key]

            features = build_features(
                history,
                context,
                point.fuel_type,
                point.tick,
                multipliers=multipliers,
            )
            if features is None:
                continue
            shape = _historical_shape(context, point.fuel_type, point)
            rows.append(features)
            targets.append(effective_multiplier(point.demand_liters, shape))
            # The rule-based baseline predicts the *median* of the recent
            # multipliers, which is what it would have produced live.
            baseline_predictions.append(_baseline_multiplier(multipliers, point.tick))

        if len(rows) < 30:
            raise LearnedModelUnavailable(
                f"not enough training data ({len(rows)} rows); using the fallback"
            )

        # Time-based split: the last 20% of rows are the validation set.
        split_index = int(len(rows) * 0.8)
        train_rows, train_targets = rows[:split_index], targets[:split_index]
        valid_rows, valid_targets = rows[split_index:], targets[split_index:]

        if not valid_rows:
            raise LearnedModelUnavailable("no validation rows; using the fallback")

        estimator = GradientBoostingRegressor(
            n_estimators=200,
            learning_rate=0.05,
            max_depth=4,
            subsample=0.9,
            random_state=42,
        )
        estimator.fit(train_rows, train_targets)

        train_predictions = estimator.predict(train_rows)
        valid_predictions = estimator.predict(valid_rows)

        # The rule-based baseline is scored on exactly the same validation
        # rows. A learned model that cannot beat the deterministic rule is
        # not worth the extra moving parts, and the selector must be able to
        # see that rather than trust the model's own opinion of itself.
        valid_baseline = baseline_predictions[split_index:]
        baseline_mae = float(mean_absolute_error(valid_targets, valid_baseline))
        validation_mae = float(mean_absolute_error(valid_targets, valid_predictions))

        metadata: dict[str, float] = {
            "validation_rows": float(len(valid_rows)),
            "validation_rmse": float(
                (
                    sum((a - b) ** 2 for a, b in zip(valid_targets, valid_predictions, strict=True))
                    / len(valid_targets)
                )
                ** 0.5
            ),
            "baseline_validation_mae": baseline_mae,
            "beats_baseline": float(validation_mae < baseline_mae),
        }

        model = LearnedModel(
            estimator=estimator,
            validation_mae=validation_mae,
            training_rows=len(train_rows),
            trained_at=datetime.now(),
            training_mae=float(mean_absolute_error(train_targets, train_predictions)),
            metadata=metadata,
        )
        self._model = model

        # The one-step error is the wrong yardstick for a recursive
        # forecast. The model is *used* over a whole horizon, so it is
        # *scored* over a whole horizon: the recursive forecast is
        # replayed over the held-out tail and the predicted total demand
        # is compared with what actually happened.
        #
        # This runs *after* the model is stored, because the validator
        # replays the real forecast path, which needs the model in place.
        horizon_scores = self._horizon_validator.score(self, history, contexts)
        if horizon_scores is not None:
            model_total_mae, baseline_total_mae = horizon_scores
            model.metadata["horizon_total_mae_liters"] = model_total_mae
            model.metadata["baseline_horizon_total_mae_liters"] = baseline_total_mae
            model.metadata["beats_baseline_at_horizon"] = float(
                model_total_mae < baseline_total_mae
            )

        logger.info(
            "learned demand model trained",
            extra={
                "training_rows": model.training_rows,
                "validation_mae": round(model.validation_mae, 4),
                "horizon_total_mae_liters": round(
                    float(model.metadata.get("horizon_total_mae_liters", 0.0)), 1
                ),
                "baseline_horizon_total_mae_liters": round(
                    float(model.metadata.get("baseline_horizon_total_mae_liters", 0.0)), 1
                ),
            },
        )
        return model

    def forecast(
        self,
        *,
        context: StationContext,
        fuel: str,
        horizon_ticks: int,
        history: list[HistoryPoint],
    ) -> Forecast:
        """Forecast the next ticks with the learned model.

        The forecast is **anchored**, not recursive: every step of the
        horizon is predicted from the *observed* history, with the
        station's current level carried forward as the reference. Feeding
        each prediction back in as the next step's input — the obvious
        recursive design — compounds every error over the horizon, and the
        model drifts back towards the mean multiplier. A station's demand
        level is a *level*, not a random walk, so the right thing to carry
        forward is the level, not the last prediction.
        """
        if self._model is None:
            raise LearnedModelUnavailable("the learned model is not trained")

        multipliers = self._recent_multipliers(history, context, fuel)
        if multipliers is None:
            raise LearnedModelUnavailable("not enough recent history to featurise the target tick")

        # The level the model believes the station is running at: the
        # median of the recent multipliers, which is the same quantity the
        # rule-based predictor holds flat.
        level = statistics.median(multipliers)

        points: list[ForecastPoint] = []

        # The whole horizon is featurised first and predicted in one
        # batch. Predicting a tick at a time costs about sixty times as
        # much: the estimator's per-call overhead dominates, and the
        # intelligence layer has to answer within a sync cycle.
        features: list[list[float]] = []
        for offset in range(1, horizon_ticks + 1):
            tick = context.current_tick + offset
            row = build_features(
                history,
                context,
                fuel,
                tick,
                lag_values=[level, level, level],
            )
            if row is None:
                raise LearnedModelUnavailable(
                    "not enough recent history to featurise the target tick"
                )
            features.append(row)

        multipliers = self._predict_rows(features)

        for offset in range(1, horizon_ticks + 1):
            tick = context.current_tick + offset
            predicted_multiplier = max(0.0, float(multipliers[offset - 1]))
            shape = context.shape_for_tick(tick, fuel)
            value = shape * predicted_multiplier

            points.append(
                ForecastPoint(
                    tick=tick,
                    sim_time=context.sim_time_for_tick(tick),
                    predicted_demand=value,
                    lower_bound=0.0,
                    upper_bound=0.0,
                    confidence=0.0,
                )
            )

        # The interval comes from the model's own validation error, scaled
        # from multiplier units into litres by the known shape.
        mae_multiplier = self._model.validation_mae
        for point in points:
            shape = context.shape_for_tick(point.tick, fuel)
            spread = max(mae_multiplier * shape, shape * 0.02)
            point.lower_bound = max(0.0, point.predicted_demand - spread)
            point.upper_bound = point.predicted_demand + spread

        confidence = self._confidence_for(points)
        for point in points:
            point.confidence = confidence

        return Forecast(
            station_id=context.station_id,
            fuel_type=fuel,
            model=ModelKind.LEARNED,
            horizon_ticks=horizon_ticks,
            points=points,
            confidence=confidence,
            needs_human_review=False,
            explanation=(
                f"Gradient-boosting model predicting the demand multiplier over "
                f"{horizon_ticks} ticks (validation MAE {mae_multiplier:.4f} "
                f"in multiplier units, {self._model.training_rows} training rows)."
            ),
        )

    def _recent_multipliers(
        self,
        history: list[HistoryPoint],
        context: StationContext,
        fuel: str,
    ) -> list[float] | None:
        """The last three observed multipliers, newest first.

        The order matters: the feature builder reads ``lag_1`` as the most
        recent observation and ``lag_3`` as the oldest. Returning them
        oldest-first would silently invert the lags and the trend feature.
        """
        multipliers = _historical_multipliers(history, context, fuel)
        if multipliers is None:
            return None
        ordered = sorted(multipliers.items())
        return [value for _, value in reversed(ordered[-3:])]

    def _predict_rows(self, rows: list[list[float]]) -> list[float]:
        """Run the estimator, converting any failure into our own error."""
        try:
            return [float(value) for value in self._model.estimator.predict(rows)]  # type: ignore[union-attr]
        except Exception as exc:
            raise LearnedModelUnavailable(f"the learned model failed to predict: {exc}") from exc

    def _confidence_for(self, points: list[ForecastPoint]) -> float:
        """Turn the model's validation error into a 0..1 confidence.

        The scale: no error is perfect confidence, and an error equal to the
        mean predicted demand is worth nothing.
        """
        if not points:
            return 0.0
        mean_demand = sum(point.predicted_demand for point in points) / len(points)
        if mean_demand <= 0:
            return 0.0

        # The interval is centred on the prediction, so half its width is
        # the error in litres.
        mae_litres = sum(point.width for point in points) / len(points) / 2.0
        confidence = 1.0 - min(1.0, mae_litres / mean_demand)
        return round(max(0.0, min(1.0, confidence)), 4)


# --- the selector ----------------------------------------------------------


@dataclass(slots=True)
class ForecastSelectorConfig:
    """Thresholds that decide when to trust the learned model."""

    #: Below this confidence the baseline is used instead.
    min_confidence: float = 0.35
    #: Below this many training rows the learned model is not trusted.
    min_training_rows: int = 30


class ForecastSelector:
    """Picks the predictor for each forecast and records why.

    The learned model is used when it is trained, has enough data, is
    confident enough, and does not raise. Otherwise the baseline answers and
    the forecast says which path was taken.
    """

    def __init__(
        self,
        *,
        learned: LearnedPredictor | None = None,
        baseline: BaselinePredictor | None = None,
        config: ForecastSelectorConfig | None = None,
    ) -> None:
        self._learned = learned or LearnedPredictor()
        self._baseline = baseline or BaselinePredictor()
        self._config = config or ForecastSelectorConfig()
        #: How many times the fallback engaged, by reason.
        self.fallback_counts: dict[str, int] = {}
        #: The reasons already counted in the current evaluation, so a
        #: fallback is counted once per cycle rather than once per
        #: forecast. Twelve forecasts a cycle would otherwise turn one
        #: degraded model into twelve "fallback activations" and bury the
        #: signal in noise.
        self._counted_reasons: set[str] = set()

    @property
    def learned(self) -> LearnedPredictor:
        """The learned predictor."""
        return self._learned

    @property
    def baseline(self) -> BaselinePredictor:
        """The baseline predictor."""
        return self._baseline

    @property
    def config(self) -> ForecastSelectorConfig:
        """The selector's thresholds."""
        return self._config

    def begin_evaluation(self) -> None:
        """Start a new evaluation cycle.

        The fallback counters are per cycle: a reason that keeps firing
        every tick is one ongoing condition, not thousands of events.
        """
        self._counted_reasons.clear()

    @property
    def active_model(self) -> ModelKind:
        """Which predictor is currently active overall.

        This mirrors :meth:`_fallback_reason` exactly: the horizon result
        is the decisive evidence, and the one-step score only decides when
        there is no horizon score. Two selectors that disagreed would mean
        the API could report one model while the platform used another.
        """
        if self._learned.is_trained:
            model = self._learned.model
            if model and model.training_rows >= self._config.min_training_rows:
                model_horizon = model.metadata.get("horizon_total_mae_liters")
                baseline_horizon = model.metadata.get("baseline_horizon_total_mae_liters")
                if (
                    model_horizon is not None
                    and baseline_horizon is not None
                    and float(model_horizon) >= float(baseline_horizon)
                ):
                    return ModelKind.BASELINE

                baseline_mae = model.metadata.get("baseline_validation_mae")
                if (
                    baseline_mae is not None
                    and model.validation_mae >= float(baseline_mae)
                    and (model_horizon is None or baseline_horizon is None)
                ):
                    return ModelKind.BASELINE

                return ModelKind.LEARNED
        return ModelKind.BASELINE

    def forecast(
        self,
        *,
        context: StationContext,
        fuel: str,
        horizon_ticks: int,
        history: list[HistoryPoint],
    ) -> Forecast:
        """Produce a forecast, falling back to the baseline when needed."""
        reason = self._fallback_reason(history)

        if reason is None:
            try:
                forecast = self._learned.forecast(
                    context=context,
                    fuel=fuel,
                    horizon_ticks=horizon_ticks,
                    history=history,
                )
                if forecast.confidence < self._config.min_confidence:
                    reason = (
                        f"learned model confidence {forecast.confidence:.2f} is below "
                        f"the {self._config.min_confidence:.2f} threshold"
                    )
                else:
                    return forecast
            except LearnedModelUnavailable as exc:
                reason = str(exc)

        baseline = self._baseline.forecast(
            context=context,
            fuel=fuel,
            horizon_ticks=horizon_ticks,
            reference_multiplier=self._baseline.reference_multiplier(
                context=context, fuel=fuel, history=history
            ),
        )
        baseline.fallback_reason = reason
        baseline.needs_human_review = True
        baseline.explanation = (
            f"{baseline.explanation} Falling back to the rule-based predictor: {reason}."
        )
        self._record_fallback(reason)
        return baseline

    def _fallback_reason(self, history: list[HistoryPoint]) -> str | None:
        """Why the learned model cannot be used, or ``None`` if it can."""
        if not self._learned.is_trained:
            return "the learned model is not trained"
        model = self._learned.model
        if model is None:
            return "the learned model is not trained"
        if model.training_rows < self._config.min_training_rows:
            return (
                f"the learned model has only {model.training_rows} training rows "
                f"(minimum {self._config.min_training_rows})"
            )
        # The decisive check comes first: the model is *used* over a whole
        # horizon, so it is *judged* over a whole horizon. A model that wins
        # one step ahead but drifts over the forecast window is worse than
        # the rule for deciding whether a station runs dry.
        #
        # This is checked before the one-step score because the one-step
        # score is the *less* representative of the two. A model can lose
        # one step ahead — a single anomalous tick in the validation split
        # is enough — and still be far better over the window the platform
        # actually forecasts. Judging it on the one-step score first would
        # throw away a model that wins on the measure that matters.
        model_horizon = model.metadata.get("horizon_total_mae_liters")
        baseline_horizon = model.metadata.get("baseline_horizon_total_mae_liters")
        if (
            model_horizon is not None
            and baseline_horizon is not None
            and float(model_horizon) >= float(baseline_horizon)
        ):
            return (
                f"the learned model does not beat the rule-based baseline over "
                f"the forecast horizon ({float(model_horizon):,.0f} L vs "
                f"{float(baseline_horizon):,.0f} L of total demand)"
            )

        # The learned model has to earn its place. On the same validation
        # rows the deterministic baseline is scored too, and a model that
        # loses to it is discarded in favour of the rule. This is the one
        # check that keeps "we used a machine learning model" from becoming
        # an excuse for a worse answer.
        #
        # It only applies when there is no horizon score to judge the model
        # by — otherwise the horizon result is the better evidence and has
        # already decided.
        baseline_mae = model.metadata.get("baseline_validation_mae")
        if (
            baseline_mae is not None
            and model.validation_mae >= float(baseline_mae)
            and (model_horizon is None or baseline_horizon is None)
        ):
            return (
                f"the learned model does not beat the rule-based baseline on the "
                f"validation split ({model.validation_mae:.4f} vs "
                f"{float(baseline_mae):.4f})"
            )
        return None

    def _record_fallback(self, reason: str | None) -> None:
        """Count a fallback activation, keyed by a normalised reason.

        Each reason is counted at most once per evaluation cycle.
        """
        key = _normalise_reason(reason)
        if key in self._counted_reasons:
            return
        self._counted_reasons.add(key)
        self.fallback_counts[key] = self.fallback_counts.get(key, 0) + 1


def _normalise_reason(reason: str | None) -> str:
    """Reduce a free-text reason to a stable metric label."""
    if reason is None:
        return "unknown"
    if "not trained" in reason:
        return "not_trained"
    if "training rows" in reason:
        return "insufficient_data"
    if "confidence" in reason:
        return "low_confidence"
    if "featurise" in reason:
        return "insufficient_history"
    if "does not beat the rule-based baseline" in reason:
        return "worse_than_baseline"
    return "model_error"


def all_fuels() -> list[str]:
    """The fuel types the forecaster covers."""
    return list(FUEL_TYPES)


def profile_for(station) -> str:
    """The demand profile of a station, defaulting to regional."""
    return get_profile(station.demand_profile).name
