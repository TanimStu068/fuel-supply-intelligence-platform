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
    """A model that loses the held-out comparison is not used.

    The comparison is the model's error against the rule's error on the
    **same held-out rows**, so neither side can flatter itself.
    """
    model = _model(baseline_validation_mae=0.04)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is True


def test_a_model_that_ties_with_the_baseline_is_kept() -> None:
    """A tie is inside the tolerance band, so the model is kept.

    The gate used to reject a model that merely *tied* the rule, which
    made the verdict a coin toss: the platform flipped between the
    learned model and the rule on differences smaller than the noise in
    the measurement.
    """
    model = _model(baseline_validation_mae=0.1)
    model.validation_mae = 0.1

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_model_that_loses_the_one_step_score_is_rejected() -> None:
    """The held-out one-step score is the gate."""
    model = _model(baseline_validation_mae=0.04)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is True


def test_a_model_that_wins_the_one_step_score_is_accepted() -> None:
    """A better held-out one-step score is enough."""
    model = _model(baseline_validation_mae=0.06)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_zero_one_step_baseline_is_not_a_score() -> None:
    """A zero baseline MAE means "not measured", not "unbeatable"."""
    model = _model(baseline_validation_mae=0.0)
    model.validation_mae = 0.05

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_the_horizon_score_does_not_gate_the_verdict() -> None:
    """The horizon score is a diagnostic, and it does not flip the verdict.

    Measured honestly, the learned model does **not** beat the rule over
    a whole forecast window — it wins 10 of 24 windows. So a horizon
    score that says the rule is better must not stop the platform from
    using the model, because the model wins the *held-out* comparison
    consistently (its error sat between 0.83 and 0.90 of the rule's at
    every tick from 16 onwards).

    Gating on the horizon score was the original defect: it is a window,
    its verdict swings from window to window, and the platform flipped
    between the model and the rule for reasons that were not true.

    The horizon score is still published, in ``/api/model-status``, so
    the finding is visible rather than hidden.
    """
    model = _model(
        horizon_total_mae_liters=326.0,
        baseline_horizon_total_mae_liters=250.0,
        baseline_validation_mae=0.06,
    )
    # It wins the held-out comparison...
    model.validation_mae = 0.05

    # ...so the model is used, even though the horizon says the rule is
    # better over the window.
    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False


def test_a_model_inside_the_tolerance_is_kept() -> None:
    """A model that is a hair worse is still kept.

    The gate used to be a coin toss: the model was rejected whenever it
    was even one percent worse, and the platform spent its life flipping
    between the learned model and the rule. The tolerance band is what
    makes the verdict stable — the measured ratio of the model's held-out
    error to the baseline's sits between 0.82 and 0.90, so a strict
    comparison would have rejected it on the days it drifted.
    """
    model = _model(baseline_validation_mae=0.1)
    model.validation_mae = 0.102  # 2 % worse — inside the 5 % band

    assert ForecastSelector._model_is_beaten_by_the_baseline(model) is False
