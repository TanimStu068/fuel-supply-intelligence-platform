import { useEffect, useState } from 'react'
import { fetchHealth, fetchState } from './api'
import type { HealthReport, StateSnapshot } from './types'

function pct(value: number): string {
  return `${(value * 100).toFixed(2)}%`
}

function litres(value: number): string {
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 0 })} L`
}

export default function App() {
  const [health, setHealth] = useState<HealthReport | null>(null)
  const [state, setState] = useState<StateSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [h, s] = await Promise.all([fetchHealth(), fetchState()])
        if (cancelled) return
        setHealth(h)
        setState(s)
        setError(null)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  if (error && !state) {
    return (
      <main className="app">
        <h1>Fuel Supply Intelligence &amp; Resilience Platform</h1>
        <p className="error">Backend unreachable: {error}</p>
      </main>
    )
  }

  return (
    <main className="app">
      <header>
        <h1>Fuel Supply Intelligence &amp; Resilience Platform</h1>
        <p className="subtitle">
          BUP CSE Fest 2026 &middot; Day 1 skeleton &middot; operator view
        </p>
      </header>

      <section className="cards">
        <article className="card">
          <h2>System status</h2>
          <dl>
            <dt>Backend API</dt>
            <dd>{health?.components.backend.status ?? '—'}</dd>
            <dt>Database</dt>
            <dd>{health?.components.database.status ?? '—'}</dd>
            <dt>Fuel Simulator</dt>
            <dd>{health?.components.simulator.status ?? '—'}</dd>
          </dl>
        </article>

        <article className="card">
          <h2>Simulation</h2>
          <dl>
            <dt>Tick</dt>
            <dd>{state?.instance.tick ?? '—'}</dd>
            <dt>Status</dt>
            <dd>{state?.instance.status ?? '—'}</dd>
            <dt>Sim time</dt>
            <dd>{state?.instance.sim_time ?? '—'}</dd>
            <dt>Scenario</dt>
            <dd>{state?.instance.scenario_id ?? '—'}</dd>
            <dt>Stale data</dt>
            <dd>{state?.stale ? 'YES — simulator served cached state' : 'no'}</dd>
          </dl>
        </article>

        <article className="card">
          <h2>Service level</h2>
          <dl>
            <dt>Service level</dt>
            <dd>{state ? pct(state.metrics.service_level) : '—'}</dd>
            <dt>Served</dt>
            <dd>{state ? litres(state.metrics.served_demand_liters) : '—'}</dd>
            <dt>Unmet</dt>
            <dd>{state ? litres(state.metrics.unmet_demand_liters) : '—'}</dd>
            <dt>Allocated</dt>
            <dd>{state ? litres(state.metrics.allocation_liters) : '—'}</dd>
            <dt>Failures</dt>
            <dd>{state?.metrics.allocation_failures ?? '—'}</dd>
          </dl>
        </article>
      </section>

      <section className="cards">
        <article className="card">
          <h2>Depots</h2>
          <table>
            <thead>
              <tr>
                <th>Depot</th>
                <th>Status</th>
                <th>Diesel</th>
                <th>Petrol</th>
                <th>Octane</th>
              </tr>
            </thead>
            <tbody>
              {state?.depots.map((depot) => (
                <tr key={depot.id}>
                  <td>{depot.name}</td>
                  <td>{depot.status}</td>
                  <td>{depot.inventory.DIESEL.toLocaleString()}</td>
                  <td>{depot.inventory.PETROL.toLocaleString()}</td>
                  <td>{depot.inventory.OCTANE.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>

        <article className="card">
          <h2>Stations</h2>
          <table>
            <thead>
              <tr>
                <th>Station</th>
                <th>Status</th>
                <th>Diesel</th>
                <th>Petrol</th>
                <th>Octane</th>
              </tr>
            </thead>
            <tbody>
              {state?.stations.map((station) => (
                <tr key={station.id}>
                  <td>{station.name}</td>
                  <td>{station.status}</td>
                  <td>{station.inventory.DIESEL.toLocaleString()}</td>
                  <td>{station.inventory.PETROL.toLocaleString()}</td>
                  <td>{station.inventory.OCTANE.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      </section>

      <section className="cards">
        <article className="card">
          <h2>Routes</h2>
          <table>
            <thead>
              <tr>
                <th>Route</th>
                <th>Depot</th>
                <th>Station</th>
                <th>Transit (ticks)</th>
                <th>Max shipment</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {state?.routes.map((route) => (
                <tr key={route.id}>
                  <td>{route.id}</td>
                  <td>{route.source_depot_id}</td>
                  <td>{route.destination_station_id}</td>
                  <td>{route.transit_ticks}</td>
                  <td>{route.max_shipment.toLocaleString()}</td>
                  <td>{route.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>

        <article className="card">
          <h2>Allocations</h2>
          <table>
            <thead>
              <tr>
                <th>Id</th>
                <th>Route</th>
                <th>Fuel</th>
                <th>Qty</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {state?.allocations.length === 0 ? (
                <tr>
                  <td colSpan={5}>No allocations yet.</td>
                </tr>
              ) : (
                state?.allocations.map((allocation) => (
                  <tr key={allocation.id}>
                    <td>{allocation.id}</td>
                    <td>{allocation.route_id}</td>
                    <td>{allocation.fuel_type}</td>
                    <td>{allocation.quantity.toLocaleString()}</td>
                    <td>{allocation.status}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </article>
      </section>

      {error && <p className="error">Last refresh failed: {error}</p>}
    </main>
  )
}
