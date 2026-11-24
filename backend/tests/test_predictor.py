"""Tests for the demand predictors and the fallback logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.intelligence.forecasting import (
    BaselinePredictor,
    ForecastSelector,
    ForecastSelectorConfig,
    HistoryPoint,
    LearnedModelUnavailable,
    ModelKind,
    StationContext,
    effective_multiplier,
)
from app.intelligence.profiles import (
    DEMAND_PROFILES,
    baseline_demand_per_tick,
    baseline_shape,
    hour_factor,
    noise_bound,
)

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _high_confidence_forecast(**kwargs):
    """A stand-in for the learned model's forecast."""
    from app.intelligence.forecasting import Forecast, ForecastPoint

    points = [
        ForecastPoint(
            tick=10 + offset,
            sim_time=BASE_TIME,
            predicted_demand=100.0,
            lower_bound=90.0,
            upper_bound=110.0,
            confidence=0.9,
        )
        for offset in range(1, 5)
    ]
    return Forecast(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        model=ModelKind.LEARNED,
        horizon_ticks=4,
        points=points,
        confidence=0.9,
        needs_human_review=False,
    )


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


def _history(count: int = 60, multiplier: float = 1.0, start_tick: int = 1) -> list[HistoryPoint]:
    """A synthetic demand history of ``count`` ticks.

    The demand follows the published profile exactly, scaled by a constant
    multiplier — which is what the simulator does before any event fires.
    The ticks run from ``start_tick`` so a series can be continued.
    """
    points: list[HistoryPoint] = []
    for offset in range(count):
        tick = start_tick + offset
        sim_time = BASE_TIME + timedelta(minutes=15 * tick)
        shape = baseline_shape(
            profile_name="urban_high",
            fuel="DIESEL",
            hour=sim_time.hour,
            region_demand_factor=1.0,
        )
        value = shape * multiplier
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


def test_the_baseline_reference_uses_the_most_recent_observations() -> None:
    """The reference window is the latest ticks, not the largest values.

    Sorting the multipliers by value and taking the top of that list picks
    the biggest spikes of the whole history, which biases every forecast
    upwards — the opposite of "recent".
    """
    predictor = BaselinePredictor()
    history = _history(count=60, multiplier=1.0)

    # A single spike early on, long before the recent window.
    spike = history[5]
    history[5] = HistoryPoint(
        station_id=spike.station_id,
        fuel_type=spike.fuel_type,
        tick=spike.tick,
        sim_time=spike.sim_time,
        demand_liters=spike.demand_liters * 50.0,
        served_liters=spike.demand_liters * 50.0,
        unmet_liters=0.0,
    )

    reference = predictor.reference_multiplier(
        context=_context(),
        fuel="DIESEL",
        history=history,
    )

    assert reference == pytest.approx(1.0, abs=0.05)


def test_the_baseline_reference_tracks_a_recent_level_change() -> None:
    """A station that recently sped up is forecast at its new level."""
    predictor = BaselinePredictor()
    history = _history(count=60, multiplier=1.0)
    history += _history(count=20, multiplier=1.4, start_tick=61)

    reference = predictor.reference_multiplier(
        context=_context(),
        fuel="DIESEL",
        history=history,
    )

    assert reference == pytest.approx(1.4, abs=0.05)


# --- the profiles ----------------------------------------------------------


def test_hour_factors_match_the_guide() -> None:
    assert hour_factor(7, "urban_high") == 1.45
    assert hour_factor(11, "urban_high") == 0.70
    assert hour_factor(6, "industrial") == 1.55
    assert hour_factor(18, "industrial") == 0.45
    assert hour_factor(6, "highway") == 1.35
    assert hour_factor(12, "highway") == 0.75
    assert hour_factor(7, "regional") == 1.25
    assert hour_factor(21, "regional") == 0.65


def test_an_unknown_profile_falls_back_to_regional() -> None:
    assert hour_factor(7, "something-else") == hour_factor(7, "regional")


def test_the_profiles_match_the_guide_table() -> None:
    assert DEMAND_PROFILES["urban_high"].daily_liters["DIESEL"] == 8500
    assert DEMAND_PROFILES["industrial"].daily_liters["DIESEL"] == 14000
    assert DEMAND_PROFILES["highway"].noise == 0.12
    assert DEMAND_PROFILES["industrial"].noise == 0.08


def test_the_daily_demand_splits_into_96_ticks() -> None:
    """The simulator spreads a day's demand over 96 ticks."""
    shape = baseline_shape(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
    )
    base = shape / 1.45  # undo the 08:00 urban_high factor

    assert base * 96 == pytest.approx(8500)


def test_baseline_demand_scales_with_the_multiplier() -> None:
    single = baseline_demand_per_tick(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
        demand_multiplier=1.0,
    )
    doubled = baseline_demand_per_tick(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
        demand_multiplier=2.0,
    )
    assert doubled == pytest.approx(single * 2)


def test_baseline_demand_applies_the_region_factor() -> None:
    base = baseline_demand_per_tick(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
        demand_multiplier=1.0,
    )
    chattogram = baseline_demand_per_tick(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.08,
        demand_multiplier=1.0,
    )
    assert chattogram == pytest.approx(base * 1.08)


def test_the_shape_excludes_the_multiplier() -> None:
    """The shape is the profile without the station's own multiplier."""
    shape = baseline_shape(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
    )
    full = baseline_demand_per_tick(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
        demand_multiplier=1.5,
    )
    assert full == pytest.approx(shape * 1.5)


def test_the_effective_multiplier_recovers_the_multiplier() -> None:
    shape = baseline_shape(
        profile_name="urban_high",
        fuel="DIESEL",
        hour=8,
        region_demand_factor=1.0,
    )
    assert effective_multiplier(shape * 1.5, shape) == pytest.approx(1.5)


def test_the_effective_multiplier_of_a_zero_shape_is_zero() -> None:
    assert effective_multiplier(100.0, 0.0) == 0.0


# --- the baseline predictor ------------------------------------------------


def test_baseline_forecasts_the_requested_horizon() -> None:
    predictor = BaselinePredictor()

    forecast = predictor.forecast(context=_context(), fuel="DIESEL", horizon_ticks=24)

    assert len(forecast.points) == 24
    assert forecast.model is ModelKind.BASELINE
    assert forecast.points[0].tick == 11
    assert forecast.points[-1].tick == 34


def test_baseline_forecast_is_positive_and_bounded() -> None:
    predictor = BaselinePredictor()

    forecast = predictor.forecast(context=_context(), fuel="DIESEL", horizon_ticks=8)

    for point in forecast.points:
        assert point.predicted_demand > 0
        assert point.lower_bound <= point.predicted_demand <= point.upper_bound
        assert point.lower_bound >= 0


def test_baseline_uses_the_profile_noise_for_its_interval() -> None:
    predictor = BaselinePredictor()

    forecast = predictor.forecast(context=_context(), fuel="DIESEL", horizon_ticks=1)

    point = forecast.points[0]
    bound = noise_bound("urban_high")
    assert point.upper_bound == pytest.approx(point.predicted_demand * (1 + bound))
    assert point.lower_bound == pytest.approx(point.predicted_demand * (1 - bound))


def test_baseline_forecast_follows_the_hour_of_day() -> None:
    predictor = BaselinePredictor()
    # 07:00 is a busy hour for urban_high, 12:00 is not.
    busy = predictor.forecast(
        context=_context(current_sim_time=datetime(2026, 1, 1, 7, 0, tzinfo=UTC)),
        fuel="DIESEL",
        horizon_ticks=1,
    )
    quiet = predictor.forecast(
        context=_context(current_sim_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        fuel="DIESEL",
        horizon_ticks=1,
    )
    assert busy.points[0].predicted_demand > quiet.points[0].predicted_demand


def test_the_hour_carries_into_the_next_hour_across_a_tick() -> None:
    """A tick at 06:45 plus 15 minutes is 07:00, which is a busy hour.

    Truncating the minutes instead of carrying them would make the
    forecast use the wrong hour factor right at the boundary.
    """
    predictor = BaselinePredictor()

    forecast = predictor.forecast(
        context=_context(current_sim_time=datetime(2026, 1, 1, 6, 45, tzinfo=UTC)),
        fuel="DIESEL",
        horizon_ticks=1,
    )

    assert forecast.points[0].sim_time.hour == 7
    busy = predictor.forecast(
        context=_context(current_sim_time=datetime(2026, 1, 1, 7, 0, tzinfo=UTC)),
        fuel="DIESEL",
        horizon_ticks=1,
    )
    assert forecast.points[0].predicted_demand == pytest.approx(busy.points[0].predicted_demand)


def test_the_baseline_holds_the_multiplier_flat_across_the_horizon() -> None:
    """The baseline's weakness: it cannot see a spike that starts later."""
    predictor = BaselinePredictor()

    forecast = predictor.forecast(
        context=_context(demand_multiplier=2.0), fuel="DIESEL", horizon_ticks=4
    )

    multipliers = [
        point.predicted_demand / _context().shape_for_tick(point.tick, "DIESEL")
        for point in forecast.points
    ]
    assert all(multiplier == pytest.approx(2.0) for multiplier in multipliers)


def test_baseline_never_needs_human_review() -> None:
    predictor = BaselinePredictor()

    forecast = predictor.forecast(context=_context(), fuel="DIESEL", horizon_ticks=4)

    assert forecast.needs_human_review is False


# --- the selector and the fallback -----------------------------------------


def test_the_selector_falls_back_when_the_model_is_untrained() -> None:
    selector = ForecastSelector()

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert forecast.fallback_reason is not None
    assert "not trained" in forecast.fallback_reason


def test_the_selector_counts_the_fallback_by_reason() -> None:
    selector = ForecastSelector()

    selector.forecast(context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history())

    assert selector.fallback_counts.get("not_trained") == 1


def test_the_selector_reports_the_baseline_as_active_when_untrained() -> None:
    selector = ForecastSelector()

    assert selector.active_model is ModelKind.BASELINE


def test_the_selector_falls_back_when_the_history_is_too_short() -> None:
    """A trained model with no usable history must still answer."""
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)

    forecast = selector.forecast(context=_context(), fuel="DIESEL", horizon_ticks=4, history=[])

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert "featurise" in forecast.fallback_reason


def test_the_selector_falls_back_when_the_model_raises() -> None:
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)

    def _boom(**kwargs):
        raise LearnedModelUnavailable("the learned model failed to predict: boom")

    selector.learned.forecast = _boom

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert selector.fallback_counts.get("model_error") == 1


def test_an_injected_prediction_fault_makes_the_model_raise() -> None:
    """The injected fault breaks the model the way a broken model breaks.

    This is what lets the fallback be *demonstrated* on a running stack
    rather than only described: the prediction component is a module
    inside the backend, so there is no container to stop.
    """
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)

    selector.learned.inject_fault("predict_error")

    with pytest.raises(LearnedModelUnavailable):
        selector.learned.forecast(
            context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
        )


def test_an_injected_prediction_fault_falls_back_through_the_selector() -> None:
    """With the fault injected, the selector serves the rule-based forecast."""
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)

    selector.learned.inject_fault("predict_error")

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert forecast.fallback_reason is not None
    assert "injected" in forecast.fallback_reason
    # A *fault* is not a *decision*: it is counted as a model error, not
    # as the kill-switch.
    assert selector.fallback_counts.get("model_error") == 1
    assert selector.fallback_counts.get("kill_switch") is None


def test_clearing_the_injected_fault_repairs_the_model() -> None:
    """Clearing the fault puts the learned model back in service."""
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)

    selector.learned.inject_fault("predict_error")
    selector.learned.inject_fault(None)

    assert selector.learned.injected_fault is None
    # The model itself is still there; it was only being broken.
    assert selector.learned.is_trained is True


def test_the_kill_switch_is_counted_as_its_own_reason() -> None:
    """An operator turning the model off is not the same as a fault.

    The fallback reason is what the Intelligence dashboard breaks down by.
    A deliberate kill-switch and a model that crashed are different
    events, and an operator reading the dashboard has to be able to tell
    them apart at a glance.
    """
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)
    selector.learned.forecast = _high_confidence_forecast

    selector.set_kill_switch(True)

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert "kill-switch" in forecast.fallback_reason
    # Not "model_error": the model did not break, it was switched off.
    assert selector.fallback_counts.get("kill_switch") == 1
    assert selector.fallback_counts.get("model_error") is None


def test_the_selector_falls_back_when_confidence_is_too_low() -> None:
    selector = ForecastSelector(
        config=ForecastSelectorConfig(min_confidence=0.99, min_training_rows=1)
    )
    selector.learned._model = _fake_trained_model(training_rows=100)
    selector.learned.forecast = _low_confidence_forecast

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert "confidence" in forecast.fallback_reason
    assert selector.fallback_counts.get("low_confidence") == 1


def test_a_learned_forecast_is_used_when_it_is_confident() -> None:
    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = _fake_trained_model(training_rows=100)
    selector.learned.forecast = _high_confidence_forecast

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.LEARNED
    assert forecast.needs_human_review is False
    assert forecast.fallback_reason is None
    assert selector.active_model is ModelKind.LEARNED


def test_the_fallback_explains_itself_in_the_forecast() -> None:
    """An operator must be able to see why the fallback engaged."""
    selector = ForecastSelector()

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert "Falling back to the rule-based predictor" in forecast.explanation
    assert "not trained" in forecast.explanation


# --- the learned model -----------------------------------------------------


def test_the_learned_model_trains_and_predicts() -> None:
    """The model learns the multiplier from the history."""
    from app.intelligence.forecasting import LearnedPredictor

    predictor = LearnedPredictor()
    history = _history(count=200, multiplier=1.0)

    model = predictor.train(history, {"station-mirpur": _context(current_tick=200)})

    assert model.training_rows > 30
    assert model.validation_mae < 0.2

    forecast = predictor.forecast(
        context=_context(current_tick=200),
        fuel="DIESEL",
        horizon_ticks=4,
        history=history,
    )

    assert forecast.model is ModelKind.LEARNED
    assert forecast.confidence > 0.5
    # The history was at multiplier 1.0, so the forecast should be too.
    assert forecast.mean_demand_per_tick == pytest.approx(
        _context(current_tick=200).shape_for_tick(201, "DIESEL"), rel=0.25
    )


def test_the_learned_model_sees_a_multiplier_step() -> None:
    """The model must react when the multiplier steps up mid-history.

    This is the demand-spike case: the baseline holds the multiplier flat
    and is wrong, while the learned model follows the step.
    """
    from app.intelligence.forecasting import LearnedPredictor

    predictor = LearnedPredictor()
    history = _history(count=100, multiplier=1.0)
    # The last 40 ticks are at double the demand.
    history.extend(_history(count=40, multiplier=2.0, start_tick=101))

    predictor.train(history, {"station-mirpur": _context(current_tick=140)})

    forecast = predictor.forecast(
        context=_context(current_tick=140, demand_multiplier=2.0),
        fuel="DIESEL",
        horizon_ticks=4,
        history=history,
    )

    shape = _context(current_tick=140).shape_for_tick(141, "DIESEL")
    assert forecast.points[0].predicted_demand > shape * 1.5


def test_the_horizon_validator_scores_the_model_the_way_it_is_used() -> None:
    """A one-step error is the wrong yardstick for a recursive forecast.

    The platform forecasts a whole day ahead, and each step feeds the
    next, so the model is judged on the total demand it predicts over the
    horizon — against the rule-based baseline on the same window.
    """
    from app.intelligence.forecasting import HorizonValidator, LearnedPredictor

    predictor = LearnedPredictor()
    history = _history(count=200, multiplier=1.0)
    contexts = {"station-mirpur": _context(current_tick=200)}
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24)
    scores = validator.score(predictor, history, contexts)

    assert scores is not None
    model_error, baseline_error = scores
    assert model_error >= 0
    assert baseline_error >= 0

    # The synthetic history follows the published profile exactly, so the
    # rule-based predictor is *exact* over the horizon. A learned model
    # cannot beat a perfect rule — which is precisely why the selector
    # has to be able to reject it.
    assert baseline_error == pytest.approx(0.0, abs=1.0)


def test_the_horizon_validator_gives_the_baseline_no_look_ahead() -> None:
    """The baseline must be scored on the history it would really have.

    The model is given the history that precedes the replay window. If the
    baseline's multiplier were taken from the *end* of that window, the
    baseline would be handed the answer it is being asked to predict, and
    every model would look worse than it is.
    """
    from app.intelligence.forecasting import HorizonValidator, LearnedPredictor

    predictor = LearnedPredictor()
    # The station speeds up exactly at the start of the replay window: the
    # last 24 ticks run at 1.4x, everything before them at 1.0.
    history = _history(count=226, multiplier=1.0)
    history += _history(count=24, multiplier=1.4, start_tick=227)
    contexts = {"station-mirpur": _context(current_tick=250)}
    predictor.train(history, contexts)

    validator = HorizonValidator(horizon_ticks=24)
    scores = validator.score(predictor, history, contexts)

    assert scores is not None
    _, baseline_error = scores

    # The honest baseline only knows the 1.0 level, so it under-forecasts
    # the window. If it were reading the end of the window it would know
    # about the 1.4x and score almost perfectly.
    assert baseline_error > 100.0


def test_the_selector_rejects_a_model_that_loses_over_the_horizon() -> None:
    """Winning one step ahead is not enough to be trusted over a horizon."""
    from app.intelligence.forecasting import LearnedModel

    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = LearnedModel(
        estimator=object(),
        validation_mae=0.1,
        training_rows=1000,
        trained_at=datetime.now(UTC),
        metadata={
            "baseline_validation_mae": 0.5,
            "horizon_total_mae_liters": 900.0,
            "baseline_horizon_total_mae_liters": 500.0,
        },
    )

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert "does not beat the rule-based baseline over" in forecast.fallback_reason


def test_the_learned_model_refuses_to_train_on_too_little_data() -> None:
    from app.intelligence.forecasting import LearnedPredictor

    predictor = LearnedPredictor()

    with pytest.raises(LearnedModelUnavailable):
        predictor.train(_history(count=5), {"station-mirpur": _context()})


# --- the learned model against the rule ------------------------------------


def test_the_baseline_is_scored_on_the_same_validation_rows() -> None:
    """The two models are compared on exactly the same data."""
    from app.intelligence.forecasting import LearnedPredictor

    predictor = LearnedPredictor()
    history = _history(count=200, multiplier=1.0)

    model = predictor.train(history, {"station-mirpur": _context(current_tick=200)})

    assert "baseline_validation_mae" in model.metadata
    assert model.metadata["baseline_validation_mae"] >= 0


def test_the_selector_falls_back_when_the_model_loses_to_the_baseline() -> None:
    """A learned model that cannot beat the deterministic rule is not used.

    On this simulator the demand shape is published, so the rule-based
    predictor is a strong baseline. The learned model has to earn its
    place against it on a held-out split.
    """
    from app.intelligence.forecasting import LearnedModel

    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = LearnedModel(
        estimator=object(),
        validation_mae=0.5,
        training_rows=1000,
        trained_at=datetime.now(UTC),
        metadata={"baseline_validation_mae": 0.1},
    )

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.BASELINE
    assert forecast.needs_human_review is True
    assert "does not beat the rule-based baseline" in forecast.fallback_reason
    assert selector.fallback_counts.get("worse_than_baseline") == 1


def test_the_horizon_result_decides_when_the_one_step_score_disagrees() -> None:
    """The horizon is the decisive measure, so it is checked first.

    A single anomalous tick in the validation split can make the model
    look worse one step ahead while it is far better over the window the
    platform actually forecasts. Judging it on the one-step score first
    would throw away the better model.
    """
    from app.intelligence.forecasting import LearnedModel

    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = LearnedModel(
        estimator=object(),
        validation_mae=0.2,  # loses one step ahead
        training_rows=1000,
        trained_at=datetime.now(UTC),
        metadata={
            "baseline_validation_mae": 0.1,
            "horizon_total_mae_liters": 100.0,  # wins over the horizon
            "baseline_horizon_total_mae_liters": 900.0,
        },
    )
    selector.learned.forecast = _high_confidence_forecast

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.LEARNED
    assert selector.active_model is ModelKind.LEARNED


def test_the_selector_keeps_a_model_that_beats_the_baseline() -> None:
    from app.intelligence.forecasting import LearnedModel

    selector = ForecastSelector(config=ForecastSelectorConfig(min_training_rows=1))
    selector.learned._model = LearnedModel(
        estimator=object(),
        validation_mae=0.1,
        training_rows=1000,
        trained_at=datetime.now(UTC),
        metadata={"baseline_validation_mae": 0.5},
    )
    selector.learned.forecast = _high_confidence_forecast

    forecast = selector.forecast(
        context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history()
    )

    assert forecast.model is ModelKind.LEARNED
    assert selector.active_model is ModelKind.LEARNED


def test_the_lags_are_newest_first() -> None:
    """The recursive forecast's lag order decides what the model sees.

    An inverted lag silently turns the trend feature backwards, which is
    exactly the kind of bug that makes a model look worse than it is.
    """
    from app.intelligence.forecasting import LearnedPredictor

    predictor = LearnedPredictor()
    history = _history(count=20, multiplier=1.0)

    recent = predictor._recent_multipliers(history, _context(), "DIESEL")

    assert recent is not None
    # The most recent observation comes first.
    assert recent[0] == pytest.approx(1.0)


def test_the_fallback_is_counted_once_per_evaluation_cycle() -> None:
    """Twelve forecasts a cycle must not become twelve fallback events."""
    selector = ForecastSelector()

    for _ in range(12):
        selector.forecast(context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history())

    assert selector.fallback_counts.get("not_trained") == 1

    selector.begin_evaluation()
    selector.forecast(context=_context(), fuel="DIESEL", horizon_ticks=4, history=_history())

    assert selector.fallback_counts.get("not_trained") == 2


def _fake_trained_model(training_rows: int):
    """A stand-in for a trained model."""
    from app.intelligence.forecasting import LearnedModel

    return LearnedModel(
        estimator=object(),
        validation_mae=1.0,
        training_rows=training_rows,
        trained_at=datetime.now(UTC),
    )


def _low_confidence_forecast(**kwargs):
    """A forecast that is not confident enough to be trusted."""
    from app.intelligence.forecasting import Forecast, ForecastPoint

    return Forecast(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        model=ModelKind.LEARNED,
        horizon_ticks=4,
        points=[
            ForecastPoint(
                tick=11,
                sim_time=BASE_TIME,
                predicted_demand=90.0,
                lower_bound=0.0,
                upper_bound=180.0,
                confidence=0.1,
            )
        ],
        confidence=0.1,
        needs_human_review=False,
    )


def _high_confidence_forecast(**kwargs):
    """A forecast that is confident enough to be trusted."""
    from app.intelligence.forecasting import Forecast, ForecastPoint

    return Forecast(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        model=ModelKind.LEARNED,
        horizon_ticks=4,
        points=[
            ForecastPoint(
                tick=11,
                sim_time=BASE_TIME,
                predicted_demand=90.0,
                lower_bound=85.0,
                upper_bound=95.0,
                confidence=0.9,
            )
        ],
        confidence=0.9,
        needs_human_review=False,
    )
