/**
 * Risk and alerts.
 *
 * The list is sorted worst-first. Selecting a row opens a drawer that
 * explains *why* the station is at risk: the signals that drove the
 * score, the forecast with its confidence band, and the recommendation
 * the intelligence layer would make.
 *
 * The point of the drawer is that no number on this screen is a black
 * box. Every one of them has something behind it.
 */

import { useState } from 'react'
import { hours, litres, percent, riskClass } from '../format'
import type { Alert, Forecast, RiskAssessment } from '../types'
import { ForecastChart } from '../components/ForecastChart'
import { EmptyState, Panel, StatusPill } from '../components/ui'

const RISK_ORDER: Record<string, number> = { CRITICAL: 0, WATCH: 1, OK: 2 }

export function RiskView({
  risks,
  alerts,
  forecasts,
}: {
  risks: RiskAssessment[]
  alerts: Alert[]
  forecasts: Forecast[]
}) {
  const [selected, setSelected] = useState<string | null>(null)

  const sorted = [...risks].sort((a, b) => {
    const byLevel = (RISK_ORDER[a.risk_level] ?? 3) - (RISK_ORDER[b.risk_level] ?? 3)
    if (byLevel !== 0) return byLevel
    return (a.hours_to_stockout ?? 999) - (b.hours_to_stockout ?? 999)
  })

  const chosen = sorted.find((risk) => `${risk.station_id}-${risk.fuel_type}` === selected) ?? null
  const chosenForecast = chosen
    ? forecasts.find(
        (forecast) =>
          forecast.station_id === chosen.station_id && forecast.fuel_type === chosen.fuel_type,
      )
    : null

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Risk &amp; alerts</h1>
          <p className="view-subtitle">
            Every shortage risk, worst first. Select a row to see the signals, the forecast
            and the reasoning behind it.
          </p>
        </div>
        <div className="view-header-stats">
          {(['CRITICAL', 'WATCH', 'OK'] as const).map((level) => (
            <span key={level} className={`pill ${riskClass(level)}`}>
              {level} {risks.filter((risk) => risk.risk_level === level).length}
            </span>
          ))}
        </div>
      </div>

      <div className="split">
        <Panel title="Shortage risk" subtitle="Select a row for the full explanation">
          {sorted.length === 0 ? (
            <EmptyState message="No risk assessments available." />
          ) : (
            <table className="table table-clickable">
              <thead>
                <tr>
                  <th>Station</th>
                  <th>Fuel</th>
                  <th>Risk</th>
                  <th>Cover</th>
                  <th>Stockout p.</th>
                  <th>Confidence</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((risk) => {
                  const key = `${risk.station_id}-${risk.fuel_type}`
                  const isSelected = key === selected
                  return (
                    <tr
                      key={key}
                      className={isSelected ? 'row-selected' : ''}
                      onClick={() => setSelected(isSelected ? null : key)}
                      tabIndex={0}
                      role="button"
                      aria-pressed={isSelected}
                      data-testid={`risk-row-${key}`}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          setSelected(isSelected ? null : key)
                        }
                      }}
                    >
                      <td>{risk.station_id}</td>
                      <td>{risk.fuel_type}</td>
                      <td>
                        <span className={`pill ${riskClass(risk.risk_level)}`}>{risk.risk_level}</span>
                      </td>
                      <td>{hours(risk.hours_to_stockout)}</td>
                      <td>{percent(risk.stockout_probability, 0)}</td>
                      <td>{percent(risk.confidence, 0)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </Panel>

        <aside className="drawer" aria-label="Risk explanation">
          {chosen ? (
            <>
              <header className="drawer-header">
                <div>
                  <h2>
                    {chosen.station_id} · {chosen.fuel_type}
                  </h2>
                  <span className={`pill ${riskClass(chosen.risk_level)}`}>{chosen.risk_level}</span>
                </div>
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => setSelected(null)}
                  aria-label="Close explanation"
                >
                  ✕
                </button>
              </header>

              <div className="drawer-body">
                <p className="drawer-lead">{chosen.recommendation}</p>

                <section className="drawer-section">
                  <h3>Why</h3>
                  <ul className="list">
                    {chosen.signals.map((signal) => (
                      <li key={signal.name} className="list-item list-item-tight">
                        <span className="list-title">{signal.name.replace(/_/g, ' ')}</span>
                        <span className="list-meta">{signal.detail}</span>
                      </li>
                    ))}
                  </ul>
                </section>

                <section className="drawer-section">
                  <h3>Forecast</h3>
                  {chosenForecast && chosenForecast.points.length > 0 ? (
                    <>
                      <ForecastChart
                        points={chosenForecast.points}
                        title={`${chosen.station_id} ${chosen.fuel_type} — predicted demand per tick`}
                      />
                      <dl className="definition-list">
                        <dt>Model</dt>
                        <dd>
                          {chosenForecast.model}
                          {chosenForecast.fallback_reason ? (
                            <span className="muted"> · fallback: {chosenForecast.fallback_reason}</span>
                          ) : null}
                        </dd>
                        <dt>Horizon</dt>
                        <dd>{chosenForecast.horizon_ticks} ticks</dd>
                        <dt>Total demand</dt>
                        <dd>{litres(chosenForecast.total_demand)}</dd>
                        <dt>Interval</dt>
                        <dd>
                          {litres(chosenForecast.lower_total)} – {litres(chosenForecast.upper_total)}
                        </dd>
                      </dl>
                      {chosenForecast.explanation ? (
                        <p className="muted">{chosenForecast.explanation}</p>
                      ) : null}
                    </>
                  ) : (
                    <p className="muted">No forecast for this station and fuel.</p>
                  )}
                </section>

                <section className="drawer-section">
                  <h3>Numbers</h3>
                  <dl className="definition-list">
                    <dt>Hours to stockout</dt>
                    <dd>{hours(chosen.hours_to_stockout)}</dd>
                    <dt>Stockout probability</dt>
                    <dd>{percent(chosen.stockout_probability, 1)}</dd>
                    <dt>Risk score</dt>
                    <dd>{chosen.risk_score.toFixed(3)}</dd>
                    <dt>Forecast demand</dt>
                    <dd>{litres(chosen.forecast_total_liters)}</dd>
                    <dt>Upper bound</dt>
                    <dd>{litres(chosen.forecast_upper_liters)}</dd>
                    <dt>Needs human review</dt>
                    <dd>{chosen.needs_human_review ? 'yes' : 'no'}</dd>
                  </dl>
                </section>
              </div>
            </>
          ) : (
            <div className="drawer-empty">
              <p className="muted">Select a risk to see why it was scored that way.</p>
            </div>
          )}
        </aside>
      </div>

      <Panel title="Alerts" subtitle="Raised by the platform and the simulator">
        {alerts.length === 0 ? (
          <EmptyState message="No alerts raised." />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Tick</th>
                <th>Severity</th>
                <th>Kind</th>
                <th>Entity</th>
                <th>Message</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {alerts.slice(0, 40).map((alert) => (
                <tr key={alert.id}>
                  <td>{alert.sim_tick}</td>
                  <td>
                    <StatusPill status={alert.severity} />
                  </td>
                  <td>{alert.kind}</td>
                  <td>{alert.entity_id}</td>
                  <td>{alert.message}</td>
                  <td>{alert.resolved ? 'resolved' : 'active'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  )
}
