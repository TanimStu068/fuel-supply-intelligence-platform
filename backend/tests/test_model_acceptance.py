"""Tests for the learned model's acceptance and rejection rules.

The platform uses the learned model only when it has earned its place.
These tests cover the rule that decides that, and — more importantly —
the case where **no rule should fire**: a model whose scores have not
been computed yet.

That case was a real bug. The horizon validator writes the model's
total error and the baseline's total error into the metadata *together*,
so a model that has been trained but not yet scored carries ``0.0`` for
both. The comparison was ``model_horizon >= baseline_horizon``, and
``0.0 >= 0.0`` is true — so a perfectly good model was rejected as
"worse than the rule-based baseline" and the platform fell back to the
baseline for reasons that were not true.

The symptom was visible in the demo: the health panel said the
prediction service was degraded, with the reason "the model does not
beat the baseline", while the model had in fact been trained and was
beating it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.intelligence.forecasting import ForecastSelector, LearnedModel


def _model(**metadata: float) -> LearnedModel:
    """A trained model with the given metadata."""
    return LearnedModel(
        estimator=object(),
        validation_mae=0.05,
        training_rows=500,
        trained_at=datetime.now(UTC),
        training_mae=0.04,
        metadata=dict(metadata),
    )


def test_a_model_with_no_scores_is_not_rejected() -> None:
    """A model that has not been scored yet is given the benefit of the doubt.

    Both horizon errors are zero because neither has been computed. That
    is "not measured", not "the model is no good".
    """
    model = _model()

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_model_with_a_zero_baseline_score_is_not_rejected() -> None:
    """A zero baseline score is missing evidence, not a perfect baseline."""
    model = _model(horizon_total_mae_liters=120.0, baseline_horizon_total_mae_liters=0.0)

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_model_that_beats_the_baseline_is_accepted() -> None:
    """A model with a lower horizon error than the baseline is used."""
    model = _model(horizon_total_mae_liters=78.1, baseline_horizon_total_mae_liters=118.1)

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_model_that_loses_to_the_baseline_is_rejected() -> None:
    """A model with a higher horizon error than the baseline is not used."""
    model = _model(horizon_total_mae_liters=118.1, baseline_horizon_total_mae_liters=78.1)

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is True


def test_a_model_that_ties_with_the_baseline_is_rejected() -> None:
    """A tie is not a win, so the rule is used instead."""
    model = _model(horizon_total_mae_liters=100.0, baseline_horizon_total_mae_liters=100.0)

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is True


def test_a_model_that_loses_the_one_step_score_is_rejected() -> None:
    """Without a horizon score, the one-step score decides."""
    model = _model(baseline_validation_mae=0.04)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is True


def test_a_model_that_wins_the_one_step_score_is_accepted() -> None:
    """Without a horizon score, a better one-step score is enough."""
    model = _model(baseline_validation_mae=0.06)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_zero_one_step_baseline_is_not_a_score() -> None:
    """A zero baseline MAE means "not measured", not "unbeatable"."""
    model = _model(baseline_validation_mae=0.0)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_the_horizon_score_decides_over_the_one_step_score() -> None:
    """The horizon is the better evidence, so it wins.

    A model can lose one step ahead — a single anomalous tick in the
    validation split is enough — and still be far better over the
    window the platform actually forecasts.
    """
    model = _model(
        horizon_total_mae_liters=78.1,
        baseline_horizon_total_mae_liters=118.1,
        baseline_validation_mae=0.04,
    )
    # It loses the one-step score...
    model.validation_mae = 0.05

    # ...and still wins, because the horizon score decides.
    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False
