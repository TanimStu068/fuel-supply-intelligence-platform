/**
 * The overview.
 *
 * The first screen an operator sees: where the simulation is, how well
 * the network is serving demand, what needs attention, and whether the
 * platform itself is healthy.
 */

import { age, litres, number, percent, riskClass, simTime } from '../format'
import type {
  Alert,
  Recommendation,
  RiskAssessment,
  ServiceHealth,
  StateSnapshot,
} from '../types'
import { EmptyState, Metric, Panel, StatusPill } from '../components/ui'

export function OverviewView({
  state,
  risks,
  alerts,
  recommendations,
  serviceHealth,
}: {
  state: StateSnapshot | null
  risks: RiskAssessment[]
  alerts: Alert[]
  recommendations: Recommendation[]
  serviceHealth: ServiceHealth | null
}) {
  const metrics = state?.metrics
  const critical = risks.filter((risk) => risk.risk_level === 'CRITICAL')
  const watch = risks.filter((risk) => risk.risk_level === 'WATCH')
  const pending = recommendations.filter((item) => item.status === 'PROPOSED')
  const activeAlerts = alerts.filter((alert) => !alert.resolved)

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Overview</h1>
          <p className="view-subtitle">
            The simulated network at a glance. Scenario {state?.instance.scenario_id ?? '—'} ·{' '}
            {state ? `${state.instance.tick_minutes}-minute ticks` : '—'} · seed{' '}
            {state?.instance.seed ?? '—'}
          </p>
        </div>
      </div>

      <div className="metric-row">
        <Metric
          label="Service level"
          value={percent(metrics?.service_level, 2)}
          hint="Served ÷ total demand"
          tone={
            metrics === undefined
              ? undefined
              : metrics.service_level >= 0.99
                ? 'good'
                : metrics.service_level >= 0.9
                  ? 'warn'
                  : 'bad'
          }
        />
        <Metric
          label="Unmet demand"
          value={litres(metrics?.unmet_demand_liters)}
          hint="Demand the network could not serve"
          tone={
            metrics && metrics.unmet_demand_liters > 0 ? 'bad' : 'good'
          }
        />
        <Metric label="Served" value={litres(metrics?.served_demand_liters)} hint="Total served" />
        <Metric
          label="Stations at CRITICAL"
          value={number(critical.length)}
          hint={`${watch.length} at WATCH`}
          tone={critical.length > 0 ? 'bad' : 'good'}
        />
        <Metric
          label="Pending approvals"
          value={number(pending.length)}
          hint="Waiting for a human"
          tone={pending.length > 0 ? 'warn' : 'good'}
        />
        <Metric
          label="Active alerts"
          value={number(activeAlerts.length)}
          hint={`${alerts.length} raised in total`}
          tone={activeAlerts.length > 0 ? 'warn' : 'good'}
        />
      </div>

      <div className="grid grid-2">
        <Panel
          title="Simulation"
          subtitle="What the organizer-provided simulator is doing right now"
        >
          <dl className="definition-list">
            <dt>Status</dt>
            <dd>
              <StatusPill status={state?.instance.status ?? 'UNKNOWN'} />
            </dd>
            <dt>Tick</dt>
            <dd>{state?.instance.tick ?? '—'}</dd>
            <dt>Simulated time</dt>
            <dd>{simTime(state?.instance.sim_time)}</dd>
            <dt>Scenario</dt>
            <dd>
              {state?.instance.scenario_id ?? '—'} (v{state?.instance.scenario_version ?? '—'})
            </dd>
            <dt>Regions</dt>
            <dd>{state?.regions.length ?? 0}</dd>
            <dt>Depots / stations / routes</dt>
            <dd>
              {state?.depots.length ?? 0} / {state?.stations.length ?? 0} /{' '}
              {state?.routes.length ?? 0}
            </dd>
            <dt>Allocations</dt>
            <dd>{state?.allocations.length ?? 0}</dd>
            <dt>Allocation failures</dt>
            <dd>{metrics?.allocation_failures ?? 0}</dd>
            <dt>Data age</dt>
            <dd>{age(state?.age_seconds)}</dd>
          </dl>
        </Panel>

        <Panel title="Platform health" subtitle="Every dependency the dashboard depends on">
          {serviceHealth ? (
            <dl className="definition-list">
              <dt>Backend</dt>
              <dd>
                <StatusPill status={serviceHealth.components.backend.status} />
              </dd>
              <dt>Database</dt>
              <dd>
                <StatusPill status={serviceHealth.components.database.status} />
              </dd>
              <dt>Simulator</dt>
              <dd>
                <StatusPill status={serviceHealth.components.simulator.status} />
              </dd>
              <dt>Prediction service</dt>
              <dd>
                <StatusPill status={serviceHealth.components.prediction_service.status} />
                <span className="muted"> model: {serviceHealth.components.prediction_service.active_model}</span>
              </dd>
              <dt>Decision engine</dt>
              <dd>
                <StatusPill status={serviceHealth.components.decision_engine.status} />
                {serviceHealth.components.decision_engine.fallback_active ? (
                  <span className="muted"> · fallback policy active</span>
                ) : null}
              </dd>
              <dt>p95 latency</dt>
              <dd>{(serviceHealth.http.p95_latency_seconds * 1000).toFixed(1)} ms</dd>
              <dt>Error rate</dt>
              <dd>{percent(serviceHealth.http.error_rate, 2)}</dd>
            </dl>
          ) : (
            <EmptyState message="The service health panel is not available." />
          )}
        </Panel>
      </div>

      <div className="grid grid-2">
        <Panel
          title="Stations needing fuel"
          subtitle="Sorted by risk — CRITICAL first"
          actions={
            <span className="muted">
              {critical.length} critical · {watch.length} watch
            </span>
          }
        >
          {risks.length === 0 ? (
            <EmptyState message="No risk assessments yet." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Station</th>
                  <th>Fuel</th>
                  <th>Risk</th>
                  <th>Cover</th>
                  <th>Stockout p.</th>
                </tr>
              </thead>
              <tbody>
                {[...risks]
                  .sort((a, b) => b.risk_score - a.risk_score)
                  .slice(0, 8)
                  .map((risk) => (
                    <tr key={`${risk.station_id}-${risk.fuel_type}`}>
                      <td>{risk.station_id}</td>
                      <td>{risk.fuel_type}</td>
                      <td>
                        <span className={`pill ${riskClass(risk.risk_level)}`}>{risk.risk_level}</span>
                      </td>
                      <td>
                        {risk.hours_to_stockout === null
                          ? '—'
                          : `${risk.hours_to_stockout.toFixed(1)} h`}
                      </td>
                      <td>{percent(risk.stockout_probability, 0)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Active alerts" subtitle="Raised by the platform, newest first">
          {activeAlerts.length === 0 ? (
            <EmptyState message="No active alerts." />
          ) : (
            <ul className="list">
              {activeAlerts.slice(0, 8).map((alert) => (
                <li key={alert.id} className="list-item">
                  <div>
                    <span className="list-title">{alert.message}</span>
                    <span className="list-meta">
                      {alert.kind} · {alert.entity_id} · tick {alert.sim_tick}
                    </span>
                  </div>
                  <StatusPill status={alert.severity} />
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  )
}
