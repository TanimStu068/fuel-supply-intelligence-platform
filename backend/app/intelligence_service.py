"""The intelligence service: runs the engine, stores the results, scores them.

This is the bridge between the intelligence layer (pure functions over the
world state) and the rest of the platform:

* it loads the stored demand history,
* runs the engine over the current cached state,
* writes the forecasts, the risk assessments and the alerts to Postgres,
* scores the previous tick's forecasts against what actually happened, so
  the prediction error is measured rather than guessed,
* updates the Prometheus series.

It never raises into the sync loop: a failure here must not stop the
platform from serving data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import Float, func, select
from sqlalchemy.dialects.postgresql import JSONB

from .config import get_settings
from .db import Database
from .intelligence.dispatcher import DispatchDecision, Dispatcher
from .intelligence.engine import IntelligenceEngine, IntelligenceResult
from .intelligence.forecasting import HistoryPoint, LearnedModelUnavailable
from .intelligence.risk import RiskLevel
from .metrics import (
    ALLOCATION_CONFLICTS,
    ALLOCATION_CONFLICTS_BY_CODE,
    ANOMALIES_DETECTED,
    BOTTLENECKS_DETECTED,
    DECISION_APPROVAL_RATE,
    DECISION_FALLBACK_ACTIVATIONS,
    DECISIONS_APPROVED,
    DECISIONS_AUTO_APPROVED,
    DECISIONS_PER_MINUTE,
    DECISIONS_PROPOSED,
    DECISIONS_REJECTED,
    DECISIONS_SUBMITTED,
    FALLBACK_ACTIVATIONS,
    FORECASTS_PRODUCED,
    ML_KILL_SWITCH_ACTIVATIONS,
    ML_MODEL_DISABLED,
    MODEL_CONFIDENCE,
    NEEDS_HUMAN_REVIEW,
    PREDICTION_ERROR_MAE,
    PREDICTION_INTERVAL_COVERAGE,
    RISK_ASSESSMENTS,
)
from .models import DemandForecast, PredictionEvaluation
from .repository import (
    read_demand_history,
    write_alert,
    write_decision_record,
    write_demand_forecast,
    write_prediction_evaluation,
    write_recommendation,
    write_risk_assessment,
)
from .state_cache import StateCache

logger = logging.getLogger(__name__)

#: The bounded set of reasons the fallback can engage for. The exact text of
#: an exception is kept in the API payload and the logs, but the Prometheus
#: label is one of these, so a failure whose message contains a changing
#: number cannot create a new time series on every poll.
FALLBACK_REASON_LABELS = (
    "intelligence_evaluation_failed",
    "no_forecast",
    "no_risk_assessment",
    "intelligence_unavailable",
)


def _fallback_label(reason: str) -> str:
    """The Prometheus label for a fallback reason.

    A known reason keeps its own name; anything else is bucketed as
    ``intelligence_evaluation_failed`` so the label cardinality stays
    bounded no matter what the failure text says.
    """
    if reason in FALLBACK_REASON_LABELS:
        return reason
    for known in FALLBACK_REASON_LABELS:
        if known in reason:
            return known
    return "intelligence_evaluation_failed"


class IntelligenceService:
    """Runs the intelligence layer over the cached state and stores the results."""

    #: How many ticks back the prediction scoring looks. A target tick is
    #: scored exactly once, so an unbounded window would only ever rescan
    #: rows that can never be scored again.
    SCORING_WINDOW_TICKS = 200

    def __init__(
        self,
        database: Database,
        cache: StateCache,
        engine: IntelligenceEngine,
        dispatcher: Dispatcher | None = None,
        client: Any | None = None,
        decision: Any | None = None,
    ) -> None:
        self._database = database
        self._cache = cache
        self._engine = engine
        self._dispatcher = dispatcher
        #: The settings, read for the prediction-error aggregation mode.
        self._settings = get_settings()
        #: The simulator client, used to read the current tick before
        #: dispatching. Optional: without it the cached tick is used.
        self._client = client
        #: The decision service, which owns the recommendations. When it
        #: is present the platform proposes allocations for a human to
        #: approve instead of shipping fuel on its own.
        self._decision = decision
        #: The most recent result, for the API to serve.
        self._last_result: IntelligenceResult | None = None
        #: The most recent dispatch decisions.
        self._last_decisions: list[DispatchDecision] = []
        #: The most recent recommendations.
        self._last_recommendations: list[Any] = []

    @property
    def engine(self) -> IntelligenceEngine:
        """The intelligence engine."""
        return self._engine

    @property
    def last_result(self) -> IntelligenceResult | None:
        """The most recent evaluation, if any."""
        return self._last_result

    @property
    def last_decisions(self) -> list[DispatchDecision]:
        """The most recent dispatch decisions."""
        return self._last_decisions

    @property
    def last_recommendations(self) -> list[Any]:
        """The most recent allocation recommendations."""
        return self._last_recommendations

    @property
    def decision(self) -> Any | None:
        """The decision service, if the platform is wired for human approval."""
        return self._decision

    def cache_state(self) -> Any | None:
        """The cached snapshot of the world, if there is one."""
        return self._cache.state

    @property
    def kill_switch(self) -> bool:
        """True when the learned model is disabled by the kill-switch."""
        return self._engine.selector.kill_switch

    def set_kill_switch(self, enabled: bool) -> bool:
        """Turn the learned demand model on or off.

        This is the operator's panic button for the model. With it on, every
        forecast is produced by the rule-based predictor, every forecast is
        flagged for human review, and the decision layer falls back to the
        days-of-cover policy.
        """
        state = self._engine.selector.set_kill_switch(enabled)
        ML_MODEL_DISABLED.set(1 if state else 0)
        ML_KILL_SWITCH_ACTIVATIONS.labels(enabled="true" if state else "false").inc()
        logger.warning(
            "the ml kill-switch was turned %s",
            "on" if state else "off",
            extra={"kill_switch": state},
        )
        # The previous result describes forecasts made with the old model
        # choice, so it is dropped: the next proposal has to re-evaluate
        # under the new setting rather than act on a stale result.
        self._last_result = None
        return state

    @property
    def injected_fault(self) -> str | None:
        """The injected prediction fault, if one is active."""
        return self._engine.selector.learned.injected_fault

    def inject_prediction_fault(self, fault: str | None) -> str | None:
        """Break the learned model on purpose, or repair it.

        The prediction component is a module inside the backend, so there
        is no container to stop: the only way to show that the fallback
        really takes over when the model *fails* — rather than when an
        operator has politely switched it off — is to break it on
        purpose.

        ``"predict_error"`` makes the model raise. ``None`` clears it.

        The difference from the kill-switch matters. The kill-switch is a
        *decision*: the model is fine and has been told not to be used.
        This is a *fault*: the model is broken, and the platform has to
        notice on its own. Both must end up in the same place — the
        rule-based predictor, with every forecast flagged for a human.
        """
        state = self._engine.selector.learned.inject_fault(fault)
        # The previous result was produced by a model that no longer
        # exists, so it is dropped: the next evaluation has to see the
        # failure for itself.
        self._last_result = None
        return state

    async def evaluate_and_store(self) -> IntelligenceResult | None:
        """Run the engine over the cached state and persist the results."""
        state = self._cache.state
        if state is None:
            return None

        try:
            history = await self._load_history()
            regions = {region.id: region.demand_factor for region in state.regions}

            # The evaluation is CPU-bound: it forecasts every station and
            # fuel, scores every risk and detects every anomaly. Run on
            # the event loop it blocks it for the whole evaluation, and
            # because the sync cycle calls this, the *API* stalls too —
            # a read that should take a millisecond waits for the
            # forecast to finish.
            #
            # ``asyncio.to_thread`` moves it to a worker thread, so the
            # event loop keeps serving requests while the forecast runs.
            # The GIL still applies, but the engine's own work releases
            # it often enough that the loop gets a look in, and the
            # measured effect is a large drop in p99.
            result = await asyncio.to_thread(
                self._engine.evaluate,
                stations=state.stations,
                depots=state.depots,
                routes=state.routes,
                allocations=state.allocations,
                supply_arrivals=state.supply_arrivals,
                region_demand_factors=regions,
                current_tick=state.instance.tick,
                current_sim_time=state.instance.sim_time,
                tick_minutes=state.instance.tick_minutes,
                history=history,
            )
        except Exception as exc:
            logger.warning("intelligence evaluation failed: %s", exc)
            # The previous result is stale now: it describes a world that
            # has moved on, and acting on it would be acting on a forecast
            # the platform could not refresh. Drop it, so the next proposal
            # has to use the fallback policy rather than an old forecast.
            self._last_result = None
            await self._recommend_with_fallback(state, reason=str(exc))
            return None

        self._last_result = result
        self._update_metrics(result)
        await self._persist(result)
        await self._score_previous_predictions()
        await self._reconcile()
        await self._recommend(result)

        return result

    async def _recommend_with_fallback(self, state: Any, *, reason: str) -> None:
        """Propose with the rule-based policy, because the model is down.

        This is the resilience path: the intelligence layer failed, but the
        platform still has to keep the network supplied. The rule is
        simple enough to trust without a model — refill the lowest
        days-of-cover first.
        """
        if self._decision is None:
            return

        from .decision.builder import build_world

        try:
            world = build_world(
                tick=state.instance.tick,
                sim_time=state.instance.sim_time,
                tick_minutes=state.instance.tick_minutes,
                depots=list(state.depots),
                stations=list(state.stations),
                routes=list(state.routes),
                allocations=list(state.allocations),
                supply_arrivals=list(state.supply_arrivals),
            )
            recommendations = self._decision.fallback.propose(
                world=world, profiles=self._station_profiles(state)
            )
        except Exception as exc:
            logger.warning("could not propose with the fallback policy: %s", exc)
            return

        await self._decision.propose(recommendations, fallback=True, reason=reason)
        self._last_recommendations = recommendations
        self._update_decision_metrics()
        await self._persist_recommendations(recommendations)

    async def _recommend(self, result: IntelligenceResult) -> None:
        """Propose allocations for the stations that need them.

        This is the human-in-the-loop path: the engine *proposes*, and
        nothing is sent to the simulator until a human approves it. The
        old dispatcher, which shipped on its own, is only used when the
        decision service is not wired in.

        When the intelligence layer cannot produce usable forecasts the
        **fallback policy** takes over: refill the lowest days-of-cover
        first, with no model behind it.
        """
        if self._decision is None:
            await self._dispatch(result)
            return

        state = self._cache.state
        if state is None:
            return

        from .decision.builder import build_world

        world = build_world(
            tick=state.instance.tick,
            sim_time=state.instance.sim_time,
            tick_minutes=state.instance.tick_minutes,
            depots=list(state.depots),
            stations=list(state.stations),
            routes=list(state.routes),
            allocations=list(state.allocations),
            supply_arrivals=list(state.supply_arrivals),
        )

        usable, reason = self._forecasts_are_usable(result)

        if usable:
            try:
                recommendations = self._decision.optimizer.propose(world=world, risks=result.risks)
            except Exception as exc:
                logger.warning("could not propose allocations: %s", exc)
                return
        else:
            logger.warning(
                "intelligence layer is degraded; using the fallback policy",
                extra={"reason": reason},
            )
            try:
                recommendations = self._decision.fallback.propose(
                    world=world, profiles=self._station_profiles(state)
                )
            except Exception as exc:
                logger.warning("could not propose with the fallback policy: %s", exc)
                return

        await self._decision.propose(recommendations, fallback=not usable, reason=reason)
        self._last_recommendations = recommendations
        self._update_decision_metrics()

        # The decision history is the audit trail, so it is written even
        # when nobody approves anything.
        await self._persist_recommendations(recommendations)

    async def propose_now(self) -> list[Any]:
        """Propose allocations right now, from the cached state.

        This is the shared path: the background sync and the operator's
        "propose" button both go through it, so both pick the same policy.
        When the intelligence layer has nothing usable, the rule-based
        fallback takes over — an operator who asks for recommendations
        while the model is down still gets valid, explained ones.
        """
        if self._decision is None:
            return []

        state = self._cache.state
        if state is None:
            return []

        from .decision.builder import build_world

        world = build_world(
            tick=state.instance.tick,
            sim_time=state.instance.sim_time,
            tick_minutes=state.instance.tick_minutes,
            depots=list(state.depots),
            stations=list(state.stations),
            routes=list(state.routes),
            allocations=list(state.allocations),
            supply_arrivals=list(state.supply_arrivals),
        )

        result = self._last_result
        usable, reason = (
            self._forecasts_are_usable(result)
            if result
            else (
                False,
                "the intelligence layer has not evaluated the network yet",
            )
        )

        if usable:
            try:
                recommendations = self._decision.optimizer.propose(world=world, risks=result.risks)
            except Exception as exc:
                logger.warning("could not propose allocations: %s", exc)
                return []
        else:
            logger.warning(
                "intelligence layer is degraded; using the fallback policy",
                extra={"reason": reason},
            )
            try:
                recommendations = self._decision.fallback.propose(
                    world=world, profiles=self._station_profiles(state)
                )
            except Exception as exc:
                logger.warning("could not propose with the fallback policy: %s", exc)
                return []

        await self._decision.propose(recommendations, fallback=not usable, reason=reason)
        self._last_recommendations = recommendations
        self._update_decision_metrics()
        await self._persist_recommendations(recommendations)
        return recommendations

    def _forecasts_are_usable(self, result: IntelligenceResult) -> tuple[bool, str]:
        """Whether the intelligence layer produced forecasts worth acting on.
        The layer falls back to the deterministic profile predictor when
        the learned model is not available — that is a *degraded* mode,
        but it is still a forecast, and it is still worth acting on. The
        decision layer's own fallback is for when there is **no** forecast
        at all: the evaluation produced nothing.
        """
        if not result.forecasts:
            return False, "the intelligence layer produced no forecast"
        if not result.risks:
            return False, "the intelligence layer produced no risk assessment"
        return True, ""

    def _station_profiles(self, state: Any) -> dict[str, Any]:
        """The demand context of each station, for the fallback policy."""
        from .decision.fallback import StationProfile

        region_factors = {region.id: region.demand_factor for region in state.regions}
        return {
            station.id: StationProfile(
                demand_profile=station.demand_profile,
                region_demand_factor=region_factors.get(station.region_id, 1.0),
                demand_multiplier=station.demand_multiplier,
            )
            for station in state.stations
        }

    async def _persist_recommendations(self, recommendations: list[Any]) -> None:
        """Write the recommendations to the decision history.

        This goes through the retrying write helper rather than a plain
        session. It writes several rows in one transaction while an
        operator may be approving a recommendation at the same time, and
        that pair is exactly what deadlocks: both hold a lock the other
        wants, and Postgres kills one side with ``40001``. The helper
        retries the whole unit of work, which the SQL standard asks for.
        """
        if not recommendations:
            return

        async def work(session) -> None:
            for recommendation in recommendations:
                await write_recommendation(
                    session,
                    recommendation=recommendation,
                    simulator_response=json.dumps(
                        recommendation.simulator_response or {}, default=str
                    ),
                )

        try:
            await self._database.write(work)
        except Exception as exc:
            logger.warning("could not persist recommendations: %s", exc)

    def _update_decision_metrics(self) -> None:
        """Publish the decision series."""
        if self._decision is None:
            return
        counters = self._decision.counters
        for name, value in counters.items():
            if name == "proposed":
                DECISIONS_PROPOSED.set(value)
            elif name == "approved":
                DECISIONS_APPROVED.set(value)
            elif name == "rejected":
                DECISIONS_REJECTED.set(value)
            elif name == "submitted":
                DECISIONS_SUBMITTED.set(value)
            elif name == "auto_approved":
                DECISIONS_AUTO_APPROVED.set(value)
            elif name == "conflicts":
                ALLOCATION_CONFLICTS.set(value)
        for code, count in self._decision.conflicts_by_code.items():
            ALLOCATION_CONFLICTS_BY_CODE.labels(code=code).set(count)

        proposed = counters.get("proposed", 0)
        approved = counters.get("approved", 0)
        DECISION_APPROVAL_RATE.set(round(approved / proposed, 4) if proposed else 0.0)
        DECISIONS_PER_MINUTE.set(self._decision.decisions_per_minute)

        fallback_reason = self._decision.consume_fallback_activation()
        if fallback_reason:
            # The label is the *kind* of failure, not its exact text, so an
            # exception message with a number in it cannot create a new
            # Prometheus time series on every poll.
            DECISION_FALLBACK_ACTIVATIONS.labels(reason=_fallback_label(fallback_reason)).inc()

    async def _reconcile(self) -> None:
        """Close the loop on the shipments the simulator has since resolved.

        The simulator is the source of truth for what happened to a
        shipment. Without this step a recommendation would sit at
        SUBMITTED forever, and the decision history would never show the
        outcome.
        """
        if self._decision is None:
            return
        state = self._cache.state
        if state is None:
            return

        reconciled = self._decision.reconcile(list(state.allocations))
        if reconciled:
            logger.info("reconciled %d allocation(s) with the simulator", reconciled)
            await self._persist_decision_history()

    async def _persist_decision_history(self) -> None:
        """Write the resolved recommendations to the audit trail.

        This goes through the retrying write helper: it writes several
        rows in one transaction, and it runs concurrently with an
        operator's approval, which is exactly the pair that deadlocks.
        """
        if self._decision is None:
            return

        async def work(session) -> None:
            for recommendation in self._decision.list_recommendations():
                await write_recommendation(
                    session,
                    recommendation=recommendation,
                    simulator_response=json.dumps(
                        recommendation.simulator_response or {}, default=str
                    ),
                )

        try:
            await self._database.write(work)
        except Exception as exc:
            logger.warning("could not persist the decision history: %s", exc)

    async def _dispatch(self, result: IntelligenceResult) -> None:
        """Move fuel to the stations that need it.

        The simulator never refills a station on its own, so this is what
        turns a risk assessment into an actual shipment.
        """
        if self._dispatcher is None or not self._dispatcher.enabled:
            return

        state = self._cache.state
        if state is None:
            return

        # The cached tick is the tick the *read* happened at. The
        # simulator has almost certainly moved on by the time we submit,
        # and two things the simulator checks are per tick: the depot's
        # dispatch allowance and the idempotency key. Planning against a
        # stale tick makes the platform believe an allowance it has
        # already spent, and the simulator answers 409.
        tick = await self._current_tick(state.instance.tick)

        try:
            decisions = await self._dispatcher.dispatch(
                risks=result.risks,
                stations={station.id: station for station in state.stations},
                depots={depot.id: depot for depot in state.depots},
                routes=list(state.routes),
                allocations=list(state.allocations),
                tick=tick,
                tick_minutes=state.instance.tick_minutes,
            )
        except Exception as exc:
            logger.warning("dispatch failed: %s", exc)
            return

        self._last_decisions = decisions
        if decisions:
            await self._persist_decisions(decisions, tick)

    async def _current_tick(self, fallback: int) -> int:
        """The simulator's current tick, read fresh.

        The cached snapshot is a few hundred milliseconds old, and the
        simulator ticks several times a second. Reading the instance right
        before dispatching costs one request and removes a whole class of
        409s that come from planning against the wrong tick.
        """
        client = getattr(self, "_client", None)
        if client is None:
            return fallback
        try:
            instance = await client.get_instance()
        except Exception as exc:
            logger.warning("could not read the current tick; using the cached one: %s", exc)
            return fallback
        return instance.tick

    async def _persist_decisions(self, decisions: list[DispatchDecision], tick: int) -> None:
        """Record every dispatch decision in the audit trail."""

        async def work(session) -> None:
            for decision in decisions:
                await write_decision_record(
                    session,
                    sim_tick=tick,
                    kind="fuel_dispatch",
                    status="submitted" if decision.submitted else "rejected",
                    rationale=decision.reason,
                    idempotency_key=(f"platform-{decision.station_id}-{decision.fuel_type}-{tick}"),
                    payload=decision.to_payload(),
                )

        try:
            await self._database.write(work)
        except Exception as exc:
            logger.warning("could not persist dispatch decisions: %s", exc)

    async def train_model(self) -> dict[str, object]:
        """Train the learned model on the stored history.

        Called by the trainer task on a schedule, and by ``POST
        /api/model/train`` so an operator can retrain on demand after a
        scenario change.

        A failure is reported, never raised: the platform keeps serving the
        rule-based forecast while the model is unavailable.
        """
        state = self._cache.state
        if state is None:
            return {"trained": False, "reason": "no state has been fetched yet"}

        history = await self._load_history()
        contexts = self._engine.build_contexts(
            stations=state.stations,
            region_demand_factors={region.id: region.demand_factor for region in state.regions},
            current_tick=state.instance.tick,
            current_sim_time=state.instance.sim_time,
            tick_minutes=state.instance.tick_minutes,
        )

        try:
            model = self._engine.selector.learned.train(history, contexts)
        except LearnedModelUnavailable as exc:
            logger.warning("could not train the learned model: %s", exc)
            return {"trained": False, "reason": str(exc)}
        except Exception as exc:
            logger.warning("model training failed unexpectedly: %s", exc)
            return {"trained": False, "reason": f"model training failed: {exc}"}

        return {
            "trained": True,
            "training_rows": model.training_rows,
            "validation_mae": round(model.validation_mae, 6),
            "training_mae": round(model.training_mae, 6),
            "trained_at": model.trained_at.isoformat(),
        }

    async def _load_history(self) -> list[HistoryPoint]:
        """Read the stored demand history for the forecaster."""
        try:
            async with self._database.session() as session:
                rows = await read_demand_history(session, limit=5000)
        except Exception as exc:
            logger.warning("could not load demand history: %s", exc)
            return []

        return [
            HistoryPoint(
                station_id=row.station_id,
                fuel_type=row.fuel_type,
                tick=row.tick,
                sim_time=row.sim_time,
                demand_liters=row.demand_liters,
                served_liters=row.served_liters,
                unmet_liters=row.unmet_liters,
            )
            for row in rows
        ]

    def _update_metrics(self, result: IntelligenceResult) -> None:
        """Publish the intelligence series."""
        for model, count in result.models_used.items():
            FORECASTS_PRODUCED.labels(model=model).inc(count)

        confidences: dict[str, list[float]] = {}
        for forecast in result.forecasts:
            confidences.setdefault(forecast.model.value, []).append(forecast.confidence)
        for model, values in confidences.items():
            if values:
                MODEL_CONFIDENCE.labels(model=model).set(sum(values) / len(values))

        for risk in result.risks:
            RISK_ASSESSMENTS.labels(level=risk.risk_level.value).inc()
            if risk.needs_human_review:
                NEEDS_HUMAN_REVIEW.labels(kind="risk").inc()

        for anomaly in result.anomalies:
            ANOMALIES_DETECTED.labels(kind=anomaly.kind.value).inc()

        for bottleneck in result.bottlenecks:
            BOTTLENECKS_DETECTED.labels(kind=bottleneck.kind.value).inc()

        for reason, count in result.fallback_activations.items():
            FALLBACK_ACTIVATIONS.labels(reason=reason).inc(count)

    async def _persist(self, result: IntelligenceResult) -> None:
        """Write the forecasts, the risks and the alerts to Postgres."""
        state = self._cache.state
        if state is None:
            return

        tick = state.instance.tick
        sim_time = state.instance.sim_time

        async def work(session) -> None:
            for forecast in result.forecasts:
                await write_demand_forecast(
                    session,
                    station_id=forecast.station_id,
                    fuel_type=forecast.fuel_type,
                    sim_tick=tick,
                    sim_time=sim_time,
                    model=forecast.model.value,
                    confidence=forecast.confidence,
                    needs_human_review=forecast.needs_human_review,
                    fallback_reason=forecast.fallback_reason,
                    horizon_ticks=forecast.horizon_ticks,
                    total_demand=forecast.total_demand,
                    lower_total=forecast.lower_total,
                    upper_total=forecast.upper_total,
                    payload=forecast.to_payload(),
                )

            for risk in result.risks:
                await write_risk_assessment(
                    session,
                    station_id=risk.station_id,
                    fuel_type=risk.fuel_type,
                    sim_tick=tick,
                    risk_level=risk.risk_level.value,
                    risk_score=risk.risk_score,
                    hours_to_stockout=risk.hours_to_stockout,
                    stockout_probability=risk.stockout_probability,
                    needs_human_review=risk.needs_human_review,
                    recommendation=risk.recommendation,
                    payload=risk.to_payload(),
                )

        try:
            await self._database.write(work)
        except Exception as exc:
            logger.warning("could not persist intelligence results: %s", exc)
            return

        await self._persist_alerts(result, tick)

    async def _persist_alerts(self, result: IntelligenceResult, tick: int) -> None:
        """Raise an alert for every risk that is not OK.

        The alert is deduplicated on ``(kind, entity_id, sim_tick)``, so a
        station that stays critical raises one alert per tick rather than
        one per poll.
        """

        async def work(session) -> None:
            for risk in result.risks:
                if risk.risk_level is RiskLevel.OK:
                    continue
                await write_alert(
                    session,
                    kind=f"shortage_{risk.risk_level.value.lower()}",
                    severity=("critical" if risk.risk_level is RiskLevel.CRITICAL else "warning"),
                    entity_type="station",
                    entity_id=risk.station_id,
                    sim_tick=tick,
                    message=risk.recommendation,
                    payload=risk.to_payload(),
                )

            for anomaly in result.anomalies:
                await write_alert(
                    session,
                    kind=f"anomaly_{anomaly.kind.value}",
                    severity=anomaly.severity,
                    entity_type="station",
                    entity_id=anomaly.entity_id,
                    sim_tick=tick,
                    message=anomaly.message,
                    payload=anomaly.to_payload(),
                )

            for bottleneck in result.bottlenecks:
                await write_alert(
                    session,
                    kind=f"bottleneck_{bottleneck.kind.value}",
                    severity=bottleneck.severity,
                    entity_type="network",
                    entity_id=bottleneck.entity_id,
                    sim_tick=tick,
                    message=bottleneck.message,
                    payload=bottleneck.to_payload(),
                )

        try:
            await self._database.write(work)
        except Exception as exc:
            logger.warning("could not persist intelligence alerts: %s", exc)

    async def _score_previous_predictions(self) -> None:
        """Score the forecasts we made for ticks that have now happened.

        This is how the prediction error is measured: the forecast for tick
        T was stored when we were at tick T-1; now that tick T has been
        observed, we compare.
        """
        state = self._cache.state
        if state is None:
            return

        current_tick = state.instance.tick

        # Only the recent window is worth scoring. Every target tick is
        # scored exactly once, so a tick that has been scored for a long
        # time cannot become scoreable again — and scanning the whole
        # history on every sync cycle grows without bound.
        window_start = max(0, current_tick - self.SCORING_WINDOW_TICKS)

        async def work(session) -> int:
            result = await session.execute(
                select(DemandForecast).where(
                    DemandForecast.sim_tick >= window_start,
                    DemandForecast.sim_tick < current_tick,
                )
            )
            forecasts = list(result.scalars())

            if not forecasts:
                return 0

            # The forecast's points are read **in the database**, not in
            # Python. Each forecast stores its whole horizon as JSON —
            # ninety-six points, about fifteen kilobytes — and the window
            # holds several hundred forecasts, so decoding them in Python
            # meant decoding megabytes of JSON on every sync cycle. The
            # instrumentation measured it at **0.23 s**, two thirds of the
            # cycle, spent decoding JSON that was then thrown away: every
            # forecast in the window had already been scored.
            #
            # Postgres is better at this than Python is. It expands the
            # JSON array server-side and hands back only the columns the
            # scoring needs, so the payload never crosses into the
            # application at all.
            point_rows = await session.execute(
                select(
                    DemandForecast.station_id,
                    DemandForecast.fuel_type,
                    DemandForecast.sim_tick,
                    DemandForecast.model,
                    func.jsonb_array_elements(DemandForecast.payload.cast(JSONB))
                    .op("->>")("tick")
                    .label("target_tick"),
                    func.jsonb_array_elements(DemandForecast.payload.cast(JSONB))
                    .op("->>")("predicted_demand")
                    .cast(Float)
                    .label("predicted_demand"),
                    func.jsonb_array_elements(DemandForecast.payload.cast(JSONB))
                    .op("->>")("lower_bound")
                    .cast(Float)
                    .label("lower_bound"),
                    func.jsonb_array_elements(DemandForecast.payload.cast(JSONB))
                    .op("->>")("upper_bound")
                    .cast(Float)
                    .label("upper_bound"),
                ).where(
                    DemandForecast.sim_tick >= window_start,
                    DemandForecast.sim_tick < current_tick,
                )
            )

            expanded_rows = point_rows.all()

            # The ticks this cycle could score. Only these are looked up
            # in ``prediction_evaluations`` — asking for "every evaluation
            # from the window onwards" reads the whole table into
            # Python, because the table holds one row per station, per
            # fuel, per tick and grows without bound. That read was
            # enough to push the container past its memory limit.
            candidate_ticks = {int(row.target_tick) for row in expanded_rows}

            if not candidate_ticks:
                return 0

            already_scored: set[tuple[str, str, int]] = set()
            if candidate_ticks:
                evaluations = await session.execute(
                    select(
                        PredictionEvaluation.station_id,
                        PredictionEvaluation.fuel_type,
                        PredictionEvaluation.target_tick,
                    ).where(PredictionEvaluation.target_tick.in_(candidate_ticks))
                )
                already_scored = {
                    (row.station_id, row.fuel_type, row.target_tick) for row in evaluations.all()
                }

            from .models import DemandObservationRow

            actuals_result = await session.execute(
                select(DemandObservationRow).where(
                    DemandObservationRow.tick >= window_start,
                    DemandObservationRow.tick <= current_tick,
                )
            )
            actuals = {
                (row.station_id, row.fuel_type, row.tick): row.demand_liters
                for row in actuals_result.scalars()
            }

            # Several forecasts may cover the same target tick — one per
            # evaluation, each made at a different lead time. The
            # honest one to score is the **latest forecast made before
            # the target tick**, because that is the one an operator
            # would have acted on. Taking an arbitrary row would make
            # the reported error depend on row order.
            #
            # The point's own numbers come back with the row, so the
            # forecast's JSON payload never has to be decoded in Python.
            best: dict[tuple[str, str, int], tuple[int, str, float, float, float]] = {}
            for row in expanded_rows:
                key = (row.station_id, row.fuel_type, int(row.target_tick))
                if key in already_scored or key not in actuals:
                    continue
                current = best.get(key)
                if current is None or row.sim_tick > current[0]:
                    best[key] = (
                        row.sim_tick,
                        row.model,
                        float(row.predicted_demand),
                        float(row.lower_bound),
                        float(row.upper_bound),
                    )

            scored = 0
            for (station_id, fuel_type, target_tick), scored_row in best.items():
                _, model, predicted_demand, lower_bound, upper_bound = scored_row
                await write_prediction_evaluation(
                    session,
                    station_id=station_id,
                    fuel_type=fuel_type,
                    target_tick=target_tick,
                    model=model,
                    predicted_demand=predicted_demand,
                    actual_demand=actuals[(station_id, fuel_type, target_tick)],
                    lower_bound=lower_bound,
                    upper_bound=upper_bound,
                )
                scored += 1

            if scored:
                await self._update_error_metrics(session)

            return scored

        try:
            await self._database.write(work)
        except Exception as exc:
            logger.warning("could not score previous predictions: %s", exc)

    async def _update_error_metrics(self, session) -> None:
        """Publish the prediction error series from the scored predictions.

        The aggregate is computed **in the database**. The table holds one
        row per scored forecast — one per station, per fuel, per tick — so
        it grows without bound, and reading it all into Python on every
        sync cycle took longer than the rest of the cycle put together.
        At six hundred thousand rows that was nine seconds of the event
        loop, which is nine seconds in which the API cannot answer.

        ``AVG`` and ``SUM`` in SQL do the same arithmetic in the place
        that already has the data, and the database does it in constant
        memory. The numbers are identical; the latency is not.

        The aggregate is also **bounded to a recent window**. Moving the
        arithmetic into SQL stopped the event loop from stalling, but it
        left the query itself scanning the whole table: at half a million
        rows a parallel sequential scan took **190 ms**, and the sync
        cycle runs every three seconds, so the aggregate was still the
        single largest item in the cycle. A metric does not need the
        whole history to be useful — it needs the recent past — so the
        query is limited to the rows scored in the last
        ``error_metric_window_ticks`` ticks.

        The window is measured in **ticks**, not seconds, because that is
        what the rows are keyed by: ``prediction_evaluations`` is one row
        per station, per fuel, per target tick, and ``target_tick`` is
        indexed. Bounding on ``created_at`` would not work — the rows are
        upserted, so a re-scored forecast keeps its original timestamp
        and the window would drift away from the data it describes.

        The window is a *window*, not a cap: the metric describes the
        model's recent behaviour, and a row from last week says nothing
        about how the model behaves now.

        The old way of doing it is still here, behind
        ``PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false``, because the
        before-and-after numbers in ``docs/load-test-report.md`` have to
        be reproducible on demand: a claim of "18x faster" that cannot
        be re-measured is a claim, not a measurement.
        """
        if not self._settings.prediction_error_aggregate_in_database:
            await self._update_error_metrics_in_python(session)
            return

        from sqlalchemy import Integer, func

        query = select(
            PredictionEvaluation.model,
            func.avg(PredictionEvaluation.absolute_error),
            func.count(PredictionEvaluation.id),
            func.sum(func.cast(PredictionEvaluation.within_interval, Integer)),
        )

        # A window of zero means "the whole table" — which is the
        # unbounded behaviour the load test found, and which
        # ``ERROR_METRIC_WINDOW_TICKS=0`` reproduces on demand.
        window_ticks = self._settings.error_metric_window_ticks
        if window_ticks > 0:
            latest = select(func.max(PredictionEvaluation.target_tick)).scalar_subquery()
            query = query.where(PredictionEvaluation.target_tick >= latest - window_ticks)

        result = await session.execute(query.group_by(PredictionEvaluation.model))
        rows = result.all()
        if not rows:
            return

        for model, mae, total, covered in rows:
            if not total:
                continue
            PREDICTION_ERROR_MAE.labels(model=model).set(float(mae))
            PREDICTION_INTERVAL_COVERAGE.labels(model=model).set(float(covered or 0) / float(total))

    async def _update_error_metrics_in_python(self, session) -> None:
        """The original implementation: read every row, then average it.

        This is the bottleneck the load test found. It is kept so the
        "before" side of the comparison can be reproduced by flipping one
        setting, and it is never used in normal operation.
        """
        result = await session.execute(select(PredictionEvaluation))
        rows = result.all()
        if not rows:
            return

        by_model: dict[str, list[Any]] = {}
        for row in rows:
            by_model.setdefault(row.model, []).append(row)

        for model, model_rows in by_model.items():
            total = len(model_rows)
            mae = sum(float(r.absolute_error) for r in model_rows) / total
            covered = sum(1 for r in model_rows if r.within_interval)
            PREDICTION_ERROR_MAE.labels(model=model).set(mae)
            PREDICTION_INTERVAL_COVERAGE.labels(model=model).set(covered / total)


def _forecast_points(forecast: DemandForecast) -> list[dict]:
    """The stored forecast points of a forecast row."""
    try:
        payload = json.loads(forecast.payload)
    except (TypeError, ValueError):
        return []
    points = payload.get("points", [])
    return [point for point in points if isinstance(point, dict)]
