/**
 * The supply and events timeline.
 *
 * Two streams on one axis: the fuel arriving at the depots, and the
 * crises the simulator is running. Both are events *in the simulated
 * world*, so they are shown against the tick rather than the wall clock.
 */

import { litres, simTime, statusClass } from '../format'
import type { SimulatorEvent, SupplyArrival } from '../types'
import { EmptyState, Panel, StatusPill } from '../components/ui'

interface TimelineEntry {
  tick: number
  kind: 'supply' | 'event'
  title: string
  detail: string
  status: string
}

export function TimelineView({
  arrivals,
  events,
}: {
  arrivals: SupplyArrival[]
  events: SimulatorEvent[]
}) {
  const entries: TimelineEntry[] = [
    ...arrivals.map((arrival) => ({
      tick: arrival.planned_tick,
      kind: 'supply' as const,
      title: `${arrival.quantity.toLocaleString()} L ${arrival.fuel_type} arrives at ${arrival.depot_id}`,
      detail:
        arrival.status === 'ARRIVED'
          ? `arrived at tick ${arrival.actual_tick}`
          : `due at tick ${arrival.planned_tick}`,
      status: arrival.status,
    })),
    ...events.map((event) => ({
      tick: event.start_tick,
      kind: 'event' as const,
      title: event.type.replace(/_/g, ' '),
      detail: `from tick ${event.start_tick} to tick ${event.end_tick}`,
      status: event.status,
    })),
  ].sort((a, b) => a.tick - b.tick)

  const incoming = arrivals.filter((arrival) => arrival.status !== 'ARRIVED')

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Supply &amp; events</h1>
          <p className="view-subtitle">
            The fuel on its way in, and the crises the simulator is running. Both are
            events in the simulated world.
          </p>
        </div>
      </div>

      <div className="grid grid-2">
        <Panel title="Incoming supply" subtitle={`${incoming.length} arrivals still to come`}>
          {arrivals.length === 0 ? (
            <EmptyState message="No supply arrivals scheduled." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Planned tick</th>
                  <th>Depot</th>
                  <th>Fuel</th>
                  <th>Quantity</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {[...arrivals]
                  .sort((a, b) => a.planned_tick - b.planned_tick)
                  .map((arrival) => (
                    <tr key={arrival.id}>
                      <td>{arrival.planned_tick}</td>
                      <td>{arrival.depot_id}</td>
                      <td>{arrival.fuel_type}</td>
                      <td>{litres(arrival.quantity)}</td>
                      <td>
                        <StatusPill
                          status={arrival.status}
                          className={statusClass(arrival.status)}
                        />
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Events" subtitle="Crises injected into the simulation">
          {events.length === 0 ? (
            <EmptyState message="No events in the simulation." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Window</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {[...events]
                  .sort((a, b) => b.start_tick - a.start_tick)
                  .map((event) => (
                    <tr key={event.id}>
                      <td>{event.type.replace(/_/g, ' ')}</td>
                      <td>
                        {event.start_tick} – {event.end_tick}
                      </td>
                      <td>
                        <StatusPill status={event.status} className={statusClass(event.status)} />
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      <Panel title="Timeline" subtitle="Everything scheduled, in tick order">
        {entries.length === 0 ? (
          <EmptyState message="Nothing scheduled." />
        ) : (
          <ol className="timeline">
            {entries.map((entry, index) => (
              <li key={`${entry.kind}-${entry.tick}-${index}`} className="timeline-item">
                <span className={`timeline-dot timeline-dot-${entry.kind}`} aria-hidden="true" />
                <div className="timeline-body">
                  <span className="timeline-tick">tick {entry.tick}</span>
                  <span className="list-title">{entry.title}</span>
                  <span className="list-meta">{entry.detail}</span>
                </div>
                <StatusPill status={entry.status} className={statusClass(entry.status)} />
              </li>
            ))}
          </ol>
        )}
      </Panel>

      <Panel title="Allocations in flight" subtitle="What the platform has sent to the simulator">
        <p className="muted">
          The allocation ledger is on the Overview and Network screens. The last simulated
          instant was {simTime(null)}.
        </p>
      </Panel>
    </div>
  )
}
