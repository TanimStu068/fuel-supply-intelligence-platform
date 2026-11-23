/**
 * The recommendations view.
 *
 * Each card is one proposed allocation, with everything the operator
 * needs to decide: the expected impact (before and after), the
 * alternatives that were considered, the confidence, the constraints
 * that were checked, and a what-if preview that runs *locally* without
 * writing anything to the simulator.
 *
 * Every card carries the banner: **simulated environment, human review
 * required**. Nothing is sent to the simulator until someone presses
 * Approve.
 */

import { useState } from 'react'
import { hours, litres, percent, riskClass } from '../format'
import type { Recommendation, SimulationResult } from '../types'
import { EmptyState, Panel, StatusPill } from '../components/ui'

export const SIMULATION_BANNER = 'Simulated environment. Human review required.'

/** How many recommendation cards are drawn before the operator asks for more. */
const PAGE_SIZE = 12

export function RecommendationCard({
  recommendation,
  onApprove,
  onReject,
  onSimulate,
  simulation,
  busy,
  actionError,
}: {
  recommendation: Recommendation
  onApprove: (id: string) => void
  onReject: (id: string) => void
  onSimulate: (id: string) => void
  simulation: SimulationResult | null
  busy: boolean
  actionError: string | null
}) {
  const projection = recommendation.projection
  const [showAlternatives, setShowAlternatives] = useState(false)

  return (
    <article className="rec-card" data-testid="recommendation-card">
      <div className="rec-banner">{SIMULATION_BANNER}</div>

      <header className="rec-header">
        <div>
          <h3>
            {recommendation.station_id} · {recommendation.fuel_type}
          </h3>
          <p className="rec-route">
            {recommendation.source_depot_id} → {recommendation.route_id}
          </p>
        </div>
        <div className="rec-header-right">
          <span className={`pill ${riskClass(recommendation.risk_level)}`}>
            {recommendation.risk_level}
          </span>
          <StatusPill status={recommendation.status} />
        </div>
      </header>

      <div className="rec-impact">
        <div className="rec-quantity">
          <span className="rec-quantity-value">{litres(recommendation.quantity)}</span>
          <span className="muted">to ship</span>
        </div>
        {projection ? (
          <div className="rec-before-after">
            <div className="rec-side">
              <span className="rec-side-label">Before</span>
              <span className={`pill ${riskClass(projection.risk_level_before)}`}>
                {projection.risk_level_before}
              </span>
              <span className="muted">{hours(projection.hours_to_stockout_before)} cover</span>
            </div>
            <span className="rec-arrow" aria-hidden="true">
              →
            </span>
            <div className="rec-side">
              <span className="rec-side-label">After</span>
              <span className={`pill ${riskClass(projection.risk_level_after)}`}>
                {projection.risk_level_after}
              </span>
              <span className="muted">{hours(projection.hours_to_stockout_after)} cover</span>
            </div>
            <div className="rec-arrival">
              <span className="muted">arrives in</span>
              <span className="rec-arrival-value">
                {projection.hours_until_arrival.toFixed(1)} h
              </span>
            </div>
          </div>
        ) : (
          <span className="muted">No projection for this recommendation.</span>
        )}
      </div>

      <p className="rec-rationale">{recommendation.rationale}</p>

      <div className="rec-meta">
        <span className="rec-meta-item">
          <span className="muted">Confidence</span> {percent(recommendation.confidence, 0)}
        </span>
        <span className="rec-meta-item">
          <span className="muted">Stockout p.</span>{' '}
          {percent(recommendation.stockout_probability, 0)}
        </span>
        {recommendation.used_fallback_policy ? (
          <span className="rec-meta-item rec-meta-warn">
            <span className="muted">Rule-based policy</span> (model unavailable)
          </span>
        ) : null}
        {recommendation.needs_human_review ? (
          <span className="rec-meta-item rec-meta-warn">
            <span className="muted">Flagged for review</span>
          </span>
        ) : null}
        <span className="rec-meta-item">
          <span className="muted">Idempotency key</span>{' '}
          <code>{recommendation.idempotency_key}</code>
        </span>
      </div>

      <details className="rec-details">
        <summary>Constraints checked ({recommendation.constraints.length})</summary>
        <ul className="constraint-list">
          {recommendation.constraints.map((check) => (
            <li key={check.code} className={check.satisfied ? 'constraint-ok' : 'constraint-bad'}>
              <span className="constraint-mark">{check.satisfied ? '✓' : '✗'}</span>
              <span className="constraint-code">{check.code}</span>
              <span className="constraint-detail">{check.detail}</span>
            </li>
          ))}
        </ul>
      </details>

      <details className="rec-details">
        <summary>Signals used ({recommendation.signals.length})</summary>
        <ul className="list">
          {recommendation.signals.map((signal) => (
            <li key={signal.name} className="list-item list-item-tight">
              <span className="list-title">{signal.name.replace(/_/g, ' ')}</span>
              <span className="list-meta">{signal.detail}</span>
            </li>
          ))}
        </ul>
      </details>

      {recommendation.alternatives.length > 0 ? (
        <div className="rec-alternatives">
          <button
            type="button"
            className="button button-ghost"
            onClick={() => setShowAlternatives((open) => !open)}
            aria-expanded={showAlternatives}
          >
            {showAlternatives ? 'Hide' : 'Show'} {recommendation.alternatives.length} alternative
            {recommendation.alternatives.length === 1 ? '' : 's'}
          </button>
          {showAlternatives ? (
            <ol className="alternative-list">
              {recommendation.alternatives.map((alternative, index) => (
                <li key={`${alternative.route_id}-${index}`} className="alternative">
                  <span className="alternative-rank">{index + 1}</span>
                  <div>
                    <span className="list-title">
                      {alternative.route_id} · {litres(alternative.quantity)}
                    </span>
                    <span className="list-meta">{alternative.reason}</span>
                  </div>
                  <span className="muted">{alternative.transit_ticks} ticks</span>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}

      {simulation ? (
        <div className="rec-simulation" data-testid="what-if-result">
          <h4>What-if (nothing was sent to the simulator)</h4>
          <dl className="definition-list">
            <dt>Writes to simulator</dt>
            <dd>{simulation.writes_to_simulator ? 'yes' : 'no'}</dd>
            <dt>Cover before</dt>
            <dd>{hours(Number(simulation.before?.hours_to_stockout ?? 0))}</dd>
            <dt>Cover after</dt>
            <dd>{hours(Number(simulation.after?.hours_to_stockout ?? 0))}</dd>
            <dt>Stockout probability</dt>
            <dd>
              {percent(Number(simulation.before?.stockout_probability ?? 0), 0)} →{' '}
              {percent(Number(simulation.after?.stockout_probability ?? 0), 0)}
            </dd>
          </dl>
        </div>
      ) : null}

      {actionError ? (
        <p className="rec-error" role="alert">
          {actionError}
        </p>
      ) : null}

      <footer className="rec-footer">
        <button
          type="button"
          className="button button-ghost"
          onClick={() => onSimulate(recommendation.id)}
          disabled={busy}
        >
          What-if preview
        </button>
        {recommendation.status === 'PROPOSED' ? (
          <>
            <button
              type="button"
              className="button button-danger"
              onClick={() => onReject(recommendation.id)}
              disabled={busy}
            >
              Reject
            </button>
            <button
              type="button"
              className="button button-primary"
              onClick={() => onApprove(recommendation.id)}
              disabled={busy}
              data-testid="approve-button"
            >
              Approve
            </button>
          </>
        ) : (
          <span className="muted">
            This recommendation is {recommendation.status.toLowerCase()}.
          </span>
        )}
      </footer>
    </article>
  )
}

export function RecommendationsView({
  recommendations,
  autoMode,
  fallbackReason,
  onApprove,
  onReject,
  onSimulate,
  onPropose,
  simulations,
  busyIds,
  actionErrors,
}: {
  recommendations: Recommendation[]
  autoMode: boolean
  fallbackReason: string | null
  onApprove: (id: string) => void
  onReject: (id: string) => void
  onSimulate: (id: string) => void
  onPropose: () => void
  simulations: Record<string, SimulationResult | null>
  busyIds: Set<string>
  actionErrors: Record<string, string | null>
}) {
  // The engine can hold hundreds of open recommendations. Rendering them
  // all would make the page unusable, so only the first page is drawn
  // and the operator asks for more.
  const [visible, setVisible] = useState(PAGE_SIZE)

  const shown = recommendations.slice(0, visible)
  const remaining = recommendations.length - shown.length

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Recommendations</h1>
          <p className="view-subtitle">
            Proposed allocations, with the expected impact. Nothing reaches the simulator
            until a human approves it.
          </p>
        </div>
        <div className="view-header-actions">
          {autoMode ? (
            <span className="pill status-warn">auto-approve ON</span>
          ) : (
            <span className="pill status-good">auto-approve OFF</span>
          )}
          <span className="muted">
            {shown.length} of {recommendations.length} shown
          </span>
          <button type="button" className="button button-ghost" onClick={onPropose}>
            Propose now
          </button>
        </div>
      </div>

      {fallbackReason ? (
        <div className="banner banner-warn" role="status">
          <span className="banner-tag">FALLBACK</span>
          <span>
            The prediction service is unavailable, so these come from the rule-based policy:{' '}
            {fallbackReason}.
          </span>
        </div>
      ) : null}

      {recommendations.length === 0 ? (
        <Panel title="No recommendations" subtitle="Nothing is waiting for a decision">
          <EmptyState message="The engine has not proposed anything yet. Press “Propose now” to ask it to." />
        </Panel>
      ) : (
        <>
          <div className="rec-grid">
            {shown.map((recommendation) => (
              <RecommendationCard
                key={recommendation.id}
                recommendation={recommendation}
                onApprove={onApprove}
                onReject={onReject}
                onSimulate={onSimulate}
                simulation={simulations[recommendation.id] ?? null}
                busy={busyIds.has(recommendation.id)}
                actionError={actionErrors[recommendation.id] ?? null}
              />
            ))}
          </div>
          {remaining > 0 ? (
            <div className="button-row">
              <button
                type="button"
                className="button button-ghost"
                onClick={() => setVisible((count) => count + PAGE_SIZE)}
              >
                Show {Math.min(PAGE_SIZE, remaining)} more ({remaining} remaining)
              </button>
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
