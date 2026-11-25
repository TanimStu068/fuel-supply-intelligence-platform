/**
 * The service health panel.
 *
 * Every dependency the platform has, in one place, with the numbers an
 * operator would check: the prediction model's identity, the decision
 * engine's counters, the request latency and the error rate.
 */

import { percent, simTime } from '../format'
import type { ServiceHealth } from '../types'
import { EmptyState, Metric, Panel, StatusPill } from '../components/ui'

/**
 * The reason the platform gave for using the rule-based predictor.
 *
 * The reasons are the platform's own words — `model_error`,
 * `not_trained`, `insufficient_history`, `worse_than_baseline` — and an
 * operator reading them should not have to look them up.
 */
const FALLBACK_REASONS: Record<string, string> = {
  model_error: 'the model raised on every forecast',
  not_trained: 'the model is not trained yet',
  insufficient_history: 'there is not enough history to trust the model',
  worse_than_baseline: 'the model is worse than the rule-based baseline',
}

function fallbackReason(reasons: Record<string, number>): string | null {
  const entries = Object.entries(reasons ?? {}).filter(([, count]) => count > 0)
  if (entries.length === 0) {
    return null
  }
  const [reason, count] = entries.sort((a, b) => b[1] - a[1])[0]
  const label = FALLBACK_REASONS[reason] ?? reason
  return count > 1 ? `${label} (${count} times)` : label
}

export function HealthView({ health }: { health: ServiceHealth | null }) {
  if (!health) {
    return (
      <div className="view">
        <div className="view-header">
          <h1>Service health</h1>
        </div>
        <Panel title="Service health">
          <EmptyState message="The service health endpoint is not answering." />
        </Panel>
      </div>
    )
  }

  const engine = health.components.decision_engine
  const prediction = health.components.prediction_service

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Service health</h1>
          <p className="view-subtitle">
            Every dependency, its state, and the numbers that say whether it is behaving.
          </p>
        </div>
      </div>

      <div className="metric-row">
        <Metric
          label="p95 latency"
          value={`${(health.http.p95_latency_seconds * 1000).toFixed(1)} ms`}
          hint="Backend request latency"
        />
        <Metric
          label="Error rate"
          value={percent(health.http.error_rate, 2)}
          hint={`${health.http.error_count} of ${health.http.request_count} requests`}
          tone={health.http.error_rate > 0 ? 'warn' : 'good'}
        />
        <Metric
          label="Decisions / min"
          value={engine.decisions_per_minute.toFixed(0)}
          hint="Recommendations in the last minute"
        />
        <Metric
          label="Approval rate"
          value={percent(engine.approval_rate, 0)}
          hint="Approved ÷ proposed"
          tone={engine.approval_rate >= 0.5 ? 'good' : 'warn'}
        />
        <Metric
          label="Auto-approve"
          value={engine.auto_mode ? 'ON' : 'OFF'}
          hint="Off by default"
          tone={engine.auto_mode ? 'warn' : 'good'}
        />
        <Metric
          label="Fallback policy"
          value={engine.fallback_active ? 'ACTIVE' : 'inactive'}
          hint={engine.fallback_reason ?? 'The model-driven policy is in use'}
          tone={engine.fallback_active ? 'warn' : 'good'}
        />
      </div>

      <div className="grid grid-2">
        <Panel title="Dependencies" subtitle="Each component and its state">
          <table className="table">
            <thead>
              <tr>
                <th>Component</th>
                <th>Status</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Backend</td>
                <td>
                  <StatusPill status={health.components.backend.status} />
                </td>
                <td className="muted">serving the dashboard</td>
              </tr>
              <tr>
                <td>Database</td>
                <td>
                  <StatusPill status={health.components.database.status} />
                </td>
                <td className="muted">
                  {health.components.database.error ?? 'Postgres is answering'}
                </td>
              </tr>
              <tr>
                <td>Simulator</td>
                <td>
                  <StatusPill status={health.components.simulator.status} />
                </td>
                <td className="muted">
                  {health.components.simulator.error ??
                    (health.components.simulator.simulation
                      ? `${health.components.simulator.simulation.status}, tick ${health.components.simulator.simulation.tick}`
                      : 'reachable')}
                </td>
              </tr>
              <tr>
                <td>Prediction service</td>
                <td>
                  <StatusPill status={prediction.status} />
                </td>
                <td className="muted">
                  model {prediction.active_model} · {prediction.forecast_count} forecasts ·
                  evaluated {simTime(prediction.evaluated_at)}
                  {prediction.fallback_active ? (
                    <>
                      {' · '}
                      <strong>
                        {fallbackReason(prediction.fallback_reasons) ??
                          'the rule-based predictor is answering'}
                      </strong>
                    </>
                  ) : null}
                </td>
              </tr>
              <tr>
                <td>Decision engine</td>
                <td>
                  <StatusPill status={engine.status} />
                </td>
                <td className="muted">
                  {engine.fallback_active
                    ? `fallback: ${engine.fallback_reason}`
                    : 'model-driven policy'}
                </td>
              </tr>
            </tbody>
          </table>
        </Panel>

        <Panel title="Decision engine" subtitle="The counters behind the decision metrics">
          <table className="table">
            <thead>
              <tr>
                <th>Counter</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(engine.counters).map(([name, value]) => (
                <tr key={name}>
                  <td>{name.replace(/_/g, ' ')}</td>
                  <td className="numeric">{value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  )
}
