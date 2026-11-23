/**
 * The decision history.
 *
 * The audit trail: every recommendation the platform made, who decided
 * it, and what the simulator then did with it. The outcome is reconciled
 * against the allocation's status, so a row shows the decision *and* the
 * consequence.
 */

import { litres, percent, riskClass, simTime } from '../format'
import type { DecisionHistoryEntry } from '../types'
import { EmptyState, Panel, StatusPill } from '../components/ui'

function outcomeTone(status: string): string {
  switch (status) {
    case 'ARRIVED':
      return 'status-good'
    case 'FAILED':
      return 'status-bad'
    case 'REJECTED':
      return 'status-muted'
    case 'SUBMITTED':
    case 'APPROVED':
      return 'status-warn'
    default:
      return 'status-muted'
  }
}

export function HistoryView({ decisions }: { decisions: DecisionHistoryEntry[] }) {
  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Decision history</h1>
          <p className="view-subtitle">
            Every recommendation, who decided it, and what the simulator did with it.
          </p>
        </div>
        <span className="muted">{decisions.length} decisions</span>
      </div>

      <Panel title="Decisions" subtitle="Newest first">
        {decisions.length === 0 ? (
          <EmptyState message="No decisions recorded yet." />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Recommendation</th>
                <th>Station / fuel</th>
                <th>Route</th>
                <th>Quantity</th>
                <th>Risk</th>
                <th>Confidence</th>
                <th>Decided by</th>
                <th>Decided at</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((decision) => (
                <tr key={decision.recommendation_id}>
                  <td>
                    <code className="code-block">{decision.recommendation_id}</code>
                  </td>
                  <td>
                    {decision.station_id}
                    <span className="muted"> · {decision.fuel_type}</span>
                  </td>
                  <td>
                    <span className="muted">{decision.route_id}</span>
                  </td>
                  <td className="numeric">{litres(decision.quantity)}</td>
                  <td>
                    <span className={`pill ${riskClass(decision.risk_level)}`}>
                      {decision.risk_level}
                    </span>
                  </td>
                  <td>{percent(decision.confidence, 0)}</td>
                  <td>{decision.decided_by ?? '—'}</td>
                  <td>{simTime(decision.decided_at)}</td>
                  <td>
                    <span className="rec-cell">
                      <StatusPill status={decision.status} className={outcomeTone(decision.status)} />
                      {decision.allocation_id !== null ? (
                        <span className="muted"> alloc {decision.allocation_id}</span>
                      ) : null}
                      {decision.error_code ? (
                        <span className="muted"> {decision.error_code}</span>
                      ) : null}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  )
}
