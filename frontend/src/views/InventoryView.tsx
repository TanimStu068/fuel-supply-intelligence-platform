/**
 * The inventory view.
 *
 * Every tank and every depot, per fuel, with a fill bar and the hours of
 * cover left. The cover is the number that matters: a tank at 40% with
 * three hours of cover is the tank that needs fuel now.
 */

import { fillFraction, hours, litres, riskClass } from '../format'
import type { Depot, FuelType, RiskAssessment, Station } from '../types'
import { FUEL_TYPES } from '../types'
import { EmptyState, FillBar, Panel } from '../components/ui'

interface Cover {
  hoursToStockout: number | null
  riskLevel: string
}

/** The cover for one (station, fuel) pair, from the risk assessments. */
function coverFor(
  risks: RiskAssessment[],
  stationId: string,
  fuel: FuelType,
): Cover {
  const match = risks.find(
    (risk) => risk.station_id === stationId && risk.fuel_type === fuel,
  )
  if (!match) return { hoursToStockout: null, riskLevel: 'OK' }
  return { hoursToStockout: match.hours_to_stockout, riskLevel: match.risk_level }
}

function toneFor(level: string): 'good' | 'warn' | 'bad' {
  if (level === 'CRITICAL') return 'bad'
  if (level === 'WATCH') return 'warn'
  return 'good'
}

export function InventoryView({
  stations,
  depots,
  risks,
}: {
  stations: Station[]
  depots: Depot[]
  risks: RiskAssessment[]
}) {
  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Inventory</h1>
          <p className="view-subtitle">
            Every tank and every depot, per fuel. The bar is the fill level; the cover is
            how long the tank lasts at the current forecast.
          </p>
        </div>
      </div>

      <Panel title="Stations" subtitle="Stock on hand against tank capacity">
        {stations.length === 0 ? (
          <EmptyState message="No stations in the current state." />
        ) : (
          <div className="inventory-grid">
            {stations.map((station) => (
              <article key={station.id} className="inventory-card">
                <header className="inventory-card-header">
                  <div>
                    <h3>{station.name}</h3>
                    <span className="muted">{station.demand_profile}</span>
                  </div>
                  <span className={`pill ${station.status === 'OUTAGE' ? 'status-bad' : 'status-good'}`}>
                    {station.status}
                  </span>
                </header>
                <table className="table table-compact">
                  <thead>
                    <tr>
                      <th>Fuel</th>
                      <th>Stock</th>
                      <th>Fill</th>
                      <th>Cover</th>
                    </tr>
                  </thead>
                  <tbody>
                    {FUEL_TYPES.map((fuel) => {
                      const onHand = station.inventory[fuel] ?? 0
                      const capacity = station.capacity[fuel] ?? 0
                      const fraction = fillFraction(onHand, capacity)
                      const cover = coverFor(risks, station.id, fuel)
                      const tone = toneFor(cover.riskLevel)
                      return (
                        <tr key={fuel}>
                          <td>{fuel}</td>
                          <td className="numeric">
                            {litres(onHand)}
                            <span className="muted"> / {litres(capacity)}</span>
                          </td>
                          <td className="fill-cell">
                            <FillBar fraction={fraction} label={`${station.name} ${fuel} fill`} tone={tone} />
                            <span className="muted">{Math.round(fraction * 100)}%</span>
                          </td>
                          <td>
                            <span className={`pill ${riskClass(cover.riskLevel)}`}>
                              {hours(cover.hoursToStockout)}
                            </span>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </article>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Depots"
        subtitle="Depot stock, and the allowance it may dispatch per tick"
      >
        {depots.length === 0 ? (
          <EmptyState message="No depots in the current state." />
        ) : (
          <div className="inventory-grid">
            {depots.map((depot) => (
              <article key={depot.id} className="inventory-card">
                <header className="inventory-card-header">
                  <div>
                    <h3>{depot.name}</h3>
                    <span className="muted">
                      dispatch allowance {litres(depot.dispatch_capacity_per_tick)} / tick
                    </span>
                  </div>
                  <span className={`pill ${depot.status === 'CONSTRAINED' ? 'status-warn' : 'status-good'}`}>
                    {depot.status}
                  </span>
                </header>
                <table className="table table-compact">
                  <thead>
                    <tr>
                      <th>Fuel</th>
                      <th>Stock</th>
                      <th>Fill</th>
                    </tr>
                  </thead>
                  <tbody>
                    {FUEL_TYPES.map((fuel) => {
                      const onHand = depot.inventory[fuel] ?? 0
                      const capacity = depot.capacity[fuel] ?? 0
                      const fraction = fillFraction(onHand, capacity)
                      const tone = fraction < 0.2 ? 'bad' : fraction < 0.4 ? 'warn' : 'good'
                      return (
                        <tr key={fuel}>
                          <td>{fuel}</td>
                          <td className="numeric">
                            {litres(onHand)}
                            <span className="muted"> / {litres(capacity)}</span>
                          </td>
                          <td className="fill-cell">
                            <FillBar
                              fraction={fraction}
                              label={`${depot.name} ${fuel} fill`}
                              tone={tone}
                            />
                            <span className="muted">{Math.round(fraction * 100)}%</span>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </article>
            ))}
          </div>
        )}
      </Panel>
    </div>
  )
}
