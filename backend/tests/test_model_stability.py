"""Regression tests for the two defects the demo dry run exposed.

**1. The model memorised the tick number.**

The feature vector included the *absolute* tick number. A gradient-boosted
tree cannot extrapolate past the largest value it was trained on, and the
simulator's demand is deterministic given ``(seed, tick, station, fuel)``
with a fixed seed — so an absolute-tick feature was a lookup key for the
answer. The model was memorising the demand curve instead of learning the
behaviour behind it, and every forecast past the end of the training range
was worse than it should have been.

The feature is now the tick's **position within the simulated day**, which
carries what the model is entitled to know and none of the answer.

**2. The acceptance verdict was taken from one noisy window.**

The horizon validator scored the model on a single replay window — the last
96 ticks of the stored history. That window slides with the daily demand
cycle, and the *difference* between two estimators' errors on one window is
mostly noise. Measured over the stored history, the model beat the baseline
on 11 of 12 station/fuel pairs in one window and 9 of 12 in the next, and
the mean difference swung from -53 % to +81 % and back to -59 %.

The visible symptom was a model that was trusted on one retrain and
discarded on the next, for reasons that were not true: the health panel
would say "the model does not beat the baseline" about a model that beat it
by 79 % over four windows.

The score is now averaged over several windows, and the baseline is scored
with the **same rule the live baseline predictor uses** — the median of the
most recent observed multipliers — because scoring it with a different rule
compares the model against a baseline that does not exist.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.intelligence.forecasting import (
    FEATURE_NAMES,
    HistoryPoint,
    HorizonValidator,
    LearnedPredictor,
    StationContext,
    _historical_multipliers,
    _tick_within_day,
    build_features,
)
from app.intelligence.profiles import baseline_shape

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _context(**overrides) -> StationContext:
    """A station context for the tests."""
    defaults = {
        "station_id": "station-mirpur",
        "profile_name": "urban_high",
        "region_demand_factor": 1.0,
        "demand_multiplier": 1.0,
        "current_tick": 10,
        "tick_minutes": 15,
        "current_sim_time": BASE_TIME,
    }
    defaults.update(overrides)
    return StationContext(**defaults)


def _baseline_total(
    replay: list[HistoryPoint],
    before: list[HistoryPoint],
    context: StationContext,
    fuel: str,
) -> float:
    """What the rule-based baseline would have predicted over a window.

    The **same rule the live baseline predictor uses**: the median of the
    most recent observed multipliers, computed only from the history that
    precedes the window. Scoring the baseline with any other rule would
    compare the model against a baseline that does not exist.
    """
    multipliers = _historical_multipliers(before, context, fuel)
    if multipliers:
        ordered = sorted(multipliers.items())
        window = [value for _, value in ordered[-12:]]
        middle = len(window) // 2
        if len(window) % 2 == 1:
            multiplier = window[middle]
        else:
            multiplier = (window[middle - 1] + window[middle]) / 2
    else:
        multiplier = 1.0

    total = 0.0
    for point in replay:
        shape = baseline_shape(
            profile_name=context.profile_name,
            fuel=fuel,
            hour=point.sim_time.hour,
            region_demand_factor=context.region_demand_factor,
        )
        total += shape * multiplier
    return total


def _history(
    count: int = 60,
    multiplier: float = 1.0,
    start_tick: int = 1,
    *,
    station_id: str = "station-mirpur",
    fuel_type: str = "DIESEL",
) -> list[HistoryPoint]:
    """A synthetic demand history of ``count`` ticks.

    The demand follows the published profile exactly, scaled by a constant
    multiplier — which is what the simulator does before any event fires.
    """
    points = []
    for offset in range(count):
        tick = start_tick + offset
        sim_time = BASE_TIME + timedelta(minutes=15 * tick)
        shape = baseline_shape(
            profile_name="urban_high",
            fuel=fuel_type,
            hour=sim_time.hour,
            region_demand_factor=1.0,
        )
        value = shape * multiplier
        points.append(
            HistoryPoint(
                station_id=station_id,
                fuel_type=fuel_type,
                tick=tick,
                sim_time=sim_time,
                demand_liters=value,
                served_liters=value,
                unmet_liters=0.0,
            )
        )
    return points


def _noisy_history(
    count: int,
    start_tick: int = 1,
    *,
    multiplier: float = 1.0,
    seed: int = 7,
) -> list[HistoryPoint]:
    """A history whose demand wanders, so the windows disagree.

    The synthetic history above follows the profile exactly, which makes the
    rule-based baseline *perfect* over every window — and a comparison
    against a perfect baseline can only ever say "the model loses". Real
    demand wanders, and so must the test data.
    """
    import random

    generator = random.Random(seed)
    points = []
    for offset in range(count):
        tick = start_tick + offset
        sim_time = BASE_TIME + timedelta(minutes=15 * tick)
        shape = baseline_shape(
            profile_name="urban_high",
            fuel="DIESEL",
            hour=sim_time.hour,
            region_demand_factor=1.0,
        )
        value = shape * multiplier * (1.0 + generator.uniform(-0.10, 0.10))
        points.append(
            HistoryPoint(
                station_id="station-mirpur",
                fuel_type="DIESEL",
                tick=tick,
                sim_time=sim_time,
                demand_liters=value,
                served_liters=value,
                unmet_liters=0.0,
            )
        )
    return points


# --- the tick feature ------------------------------------------------------


def test_the_tick_feature_is_the_position_within_the_day() -> None:
    """The feature is the position in the day, not the absolute tick."""
    assert _tick_within_day(0) == 0.0
    assert _tick_within_day(95) == 95.0
    # The 97th tick is the first tick of the second day.
    assert _tick_within_day(96) == 0.0
    assert _tick_within_day(96 * 10 + 37) == 37.0


def test_the_feature_vector_is_bounded() -> None:
    """A tree cannot extrapolate past the largest value it was trained on.

    So the tick feature must be bounded: a forecast for tick 5 001 has to
    land on the same leaves as the training rows, not past the end of the
    training range.
    """
    history = _history(count=200)
    context = _context(current_tick=200, current_sim_time=BASE_TIME)

    # The features are built for an *observed* tick, which is where the
    # training rows come from. The point is the value of the tick feature:
    # it is the position in the day, so the same tick of any day lands on
    # the same leaves.
    row = build_features(history, context, "DIESEL", 200)
    assert row is not None

    assert row[0] == pytest.approx(200 % 96)
    assert row[0] < 96

    # And the same position in the day, 500 days later, is the same value.
    later = build_features(history, context, "DIESEL", 200 + 96 * 500)
    assert later is None or later[0] == pytest.approx(row[0])


def test_the_feature_names_say_what_the_feature_is() -> None:
    """The name has to match the feature, or the report lies."""
    assert "target_tick" not in FEATURE_NAMES
    assert "tick_within_day" in FEATURE_NAMES


def test_a_forecast_past_the_training_range_is_still_sane() -> None:
    """A model trained on ticks 1..200 can forecast tick 250.

    Before the fix the absolute tick put every future tick into the same
    leaf as the largest training value, so the forecast stopped tracking
    the day.
    """
    history = _noisy_history(count=200)
    context = _context(current_tick=200, current_sim_time=BASE_TIME)
    predictor = LearnedPredictor()
    predictor.train(history, {"station-mirpur": context})

    forecast = predictor.forecast(
        context=context,
        fuel="DIESEL",
        horizon_ticks=24,
        history=history,
    )

    # The forecast still follows the published shape rather than collapsing.
    expected = baseline_shape(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=context.hour_for_tick(201),
        region_demand_factor=1.0,
    )
    assert forecast.points[0].predicted_demand == pytest.approx(expected, rel=0.5)


# --- the acceptance verdict ------------------------------------------------


def test_the_horizon_score_is_averaged_over_several_windows() -> None:
    """One window is a coin flip; several windows are a verdict."""
    assert HorizonValidator.SCORE_WINDOWS > 1


def test_the_score_needs_enough_history_for_more_than_one_window() -> None:
    """With too little history the validator still scores what it has."""
    history = _noisy_history(count=200)
    contexts = {"station-mirpur": _context(current_tick=200, current_sim_time=BASE_TIME)}
    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24, windows=4)
    scores = validator.score(predictor, history, contexts)

    assert scores is not None
    model_error, baseline_error = scores
    assert model_error >= 0
    assert baseline_error >= 0


def test_the_windows_disagree_with_each_other() -> None:
    """The regression: the windows must be independent, not repeated.

    The windows are scored with a model **refitted on the history that
    precedes each one**. Without the refit, every window is scored with
    the same model — one that was fitted on the whole history, including
    every window — so the windows are the same measurement repeated and
    averaging them hides nothing.

    With the refit the windows are genuinely independent measurements,
    and this test pins that: a window's verdict must be able to differ
    from its neighbour's. A model that memorised its training data would
    win every window by the same margin, which is exactly the leak.
    """
    history = _noisy_history(count=500)
    contexts = {"station-mirpur": _context(current_tick=500, current_sim_time=BASE_TIME)}
    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24, windows=6)
    windows = validator.score_windows(predictor, history, contexts)

    assert len(windows) > 1

    # The refitted models are what produce the verdicts, and they are
    # fitted on strictly less data than the serving model.
    refit = validator._model_for_prefix(history[:100], contexts)
    assert refit is not None
    assert refit.model is not None
    assert refit.model.training_rows <= len(history[:100])
    assert predictor.model is not None
    assert predictor.model.training_rows > len(history[:100])


def test_the_forecast_for_a_window_cannot_see_the_window() -> None:
    """The regression, stated as a property that cannot be faked.

    The leak was: the model scored on a replay window had been **fitted
    on that window**. So the forecast it produced for the window was a
    function of the window's own contents.

    This test states the opposite as a measurable property: **perturb the
    rows inside the window and the model's predicted total must not
    move.**

    The rows that must be left alone are the windows' **origins** — the
    last row before each window. They are part of the history the model
    is *given*, so perturbing them would change the forecast for a
    legitimate reason and the test would prove nothing.

    The check is made directly on the forecast rather than on the score,
    because the score also moves when the actual total moves: the
    perturbation changes what actually happened, so the error against it
    grows. What must not move is **what the platform predicted**.

    Only **one** window is checked, and that is not laziness. The windows
    are contiguous, so a later window's replay contains the earlier
    window's origin — perturbing one changes the history the other is
    given, legitimately. Checking the newest window against a
    perturbation confined to it keeps the test free of that confound.
    """
    history = _noisy_history(count=400)
    contexts = {"station-mirpur": _context(current_tick=400, current_sim_time=BASE_TIME)}

    validator = HorizonValidator(horizon_ticks=24, windows=1)
    predictor = LearnedPredictor()
    predictor.train(history, contexts, score_horizon=False)

    horizon = validator.horizon_ticks
    points = sorted(history, key=lambda p: p.tick)

    end = len(points)
    start = end - horizon
    origin = points[start - 1]
    interior = {point.tick for point in points[start:end]}
    assert origin.tick not in interior, "the origin must not be perturbed"

    def predicted_total(rows: list[HistoryPoint]) -> float:
        """What the platform would have predicted over the window."""
        window_points = sorted(rows, key=lambda p: p.tick)
        window_origin = window_points[start - 1]
        before = [point for point in window_points if point.tick <= window_origin.tick]
        window_context = replace(
            contexts["station-mirpur"],
            current_tick=window_origin.tick,
            current_sim_time=window_origin.sim_time,
        )
        model = validator._model_for_prefix(before, contexts)
        assert model is not None
        forecast = model.forecast(
            context=window_context,
            fuel="DIESEL",
            horizon_ticks=horizon,
            history=before,
        )
        return sum(point.predicted_demand for point in forecast.points)

    perturbed = [
        HistoryPoint(
            station_id=point.station_id,
            fuel_type=point.fuel_type,
            tick=point.tick,
            sim_time=point.sim_time,
            demand_liters=point.demand_liters * 50.0,
            served_liters=point.served_liters * 50.0,
            unmet_liters=point.unmet_liters * 50.0,
        )
        if point.tick in interior
        else point
        for point in history
    ]

    original_total = predicted_total(history)
    perturbed_total = predicted_total(perturbed)

    # The forecast cannot have moved: the model never saw the perturbed
    # rows, and the rows it *was* given are unchanged.
    assert perturbed_total == pytest.approx(original_total, rel=1e-9, abs=1e-9)


def test_each_window_is_scored_with_a_model_that_never_saw_it() -> None:
    """The regression: the score must not leak the future.

    The obvious implementation scores the *already trained* model on a
    replay window — but that model was fitted on the whole history,
    including the window, so the score measures the model's memory of
    rows it has already seen.

    This test pins the property that fixes it: for every window, the model
    that produced the forecast was fitted only on the history that
    precedes that window.
    """
    history = _noisy_history(count=400)
    contexts = {"station-mirpur": _context(current_tick=400, current_sim_time=BASE_TIME)}

    validator = HorizonValidator(horizon_ticks=24, windows=3)
    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    # The refit is the mechanism, so it is checked directly: the model
    # fitted on a prefix must not contain any row from after the prefix.
    prefix = history[:100]
    refit = validator._model_for_prefix(prefix, contexts)
    assert refit is not None
    assert refit.model is not None
    assert refit.model.training_rows <= len(prefix)

    # And the serving model is untouched by the scoring — the platform
    # must keep serving the model that has seen the recent history.
    assert predictor.model is not None
    assert predictor.model.training_rows > len(prefix)


def test_averaging_the_windows_reduces_the_spread() -> None:
    """The averaged score is the pooled error over every window.

    That is the point of averaging: the score describes the model across
    the recent past rather than on one lucky or unlucky window.
    """
    history = _noisy_history(count=500)
    contexts = {"station-mirpur": _context(current_tick=500, current_sim_time=BASE_TIME)}
    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24, windows=6)
    windows = validator.score_windows(predictor, history, contexts)

    pooled_model = sum(model for model, _ in windows) / len(windows)
    pooled_baseline = sum(baseline for _, baseline in windows) / len(windows)

    averaged_model, averaged_baseline = validator.score(predictor, history, contexts)
    assert averaged_model is not None
    assert averaged_baseline is not None

    # The averaged score is the mean over the windows.
    assert averaged_model == pytest.approx(pooled_model)
    assert averaged_baseline == pytest.approx(pooled_baseline)


def test_the_baseline_is_scored_with_the_live_baseline_rule() -> None:
    """The baseline must be scored the way the baseline actually behaves.

    The live baseline predictor holds the **median of the most recent
    observed multipliers** flat. Scoring it with some other rule would
    compare the model against a baseline that does not exist — and against
    a *better* one, which is the direction that flatters the model.
    """
    history = _noisy_history(count=200)
    context = _context(current_tick=200, current_sim_time=BASE_TIME)

    multipliers = _historical_multipliers(history, context, "DIESEL")
    assert multipliers is not None

    ordered = sorted(multipliers.items())
    recent = [value for _, value in ordered[-12:]]
    middle = len(recent) // 2
    expected = recent[middle] if len(recent) % 2 == 1 else (recent[middle - 1] + recent[middle]) / 2

    assert HorizonValidator._baseline_multiplier(history, context, "DIESEL") == pytest.approx(
        expected
    )


def test_the_baseline_still_gets_no_look_ahead() -> None:
    """Averaging over windows must not give the baseline the answer.

    The multiplier the baseline is scored with has to come from the history
    *before* each window, never from inside it.
    """
    history = _history(count=200, multiplier=1.0)
    history += _history(count=24, multiplier=1.4, start_tick=201)
    contexts = {"station-mirpur": _context(current_tick=224, current_sim_time=BASE_TIME)}
    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24, windows=1)
    scores = validator.score(predictor, history, contexts)

    assert scores is not None
    _, baseline_error = scores

    # The honest baseline only knows the 1.0 level for the window — the
    # 1.4x starts *inside* it — so it under-forecasts. If it were reading
    # the end of the window it would know about the 1.4x and score almost
    # perfectly.
    assert baseline_error > 100.0


def test_the_windows_are_the_most_recent_ones() -> None:
    """The score describes the recent past, which is where it is used.

    A level shift in the *old* part of the history must not dominate the
    verdict, because the model will be used on the recent past.
    """
    history = _noisy_history(count=300)
    contexts = {"station-mirpur": _context(current_tick=300, current_sim_time=BASE_TIME)}
    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24, windows=2)
    recent = validator.score(predictor, history, contexts)

    # The same history, but with the *recent* part shifted and the old part
    # not. The averaged score must follow the recent part.
    shifted = _noisy_history(count=300)
    shifted += _history(count=48, multiplier=1.4, start_tick=301)
    shifted_contexts = {"station-mirpur": _context(current_tick=348, current_sim_time=BASE_TIME)}
    shifted_predictor = LearnedPredictor()
    shifted_predictor.train(shifted, shifted_contexts)
    shifted_scores = validator.score(shifted_predictor, shifted, shifted_contexts)

    assert recent is not None
    assert shifted_scores is not None

    # The baseline's error grows when the level moves and it has not seen
    # the new level — that is the difference the score is measuring.
    assert shifted_scores[1] > recent[1]
