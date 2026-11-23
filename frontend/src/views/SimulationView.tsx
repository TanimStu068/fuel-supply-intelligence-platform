/**
 * The simulation controls and the crisis-injection panel.
 *
 * These drive the *simulator*, not the platform. The panel is labelled as
 * such in three places — the heading, a banner, and the buttons — because
 * an operator must never confuse "run the simulation forward" with
 * "ship fuel".
 */

import { useState } from 'react'
import { simTime } from '../format'
import type { SimulationControl } from '../types'
import { EmptyState, Panel, StatusPill } from '../components/ui'

/** The six crisis types the simulator accepts (guide §7.8). */
const CRISES = [
  {
    type: 'demand_spike',
    label: 'Demand spike',
    hint: 'Demand rises at one or more stations.',
    parameters: { multiplier: 2.0, station_ids: [] },
  },
  {
    type: 'route_disruption',
    label: 'Route disruption',
    hint: 'A route stops being usable.',
    parameters: { route_ids: [] },
  },
  {
    type: 'station_outage',
    label: 'Station outage',
    hint: 'A station cannot serve or receive fuel.',
    parameters: { station_ids: [] },
  },
  {
    type: 'depot_constraint',
    label: 'Depot constraint',
    hint: 'A depot is put under a constraint.',
    parameters: { depot_ids: [] },
  },
  {
    type: 'shipment_delay',
    label: 'Shipment delay',
    hint: 'Fuel already in transit arrives late.',
    parameters: { delay_ticks: 4 },
  },
  {
    type: 'supply_shortfall',
    label: 'Supply shortfall',
    hint: 'A scheduled supply arrival is cut short.',
    parameters: { reduction_ratio: 0.5 },
  },
]

export function SimulationView({
  control,
  onControl,
  onCrisis,
  busy,
  error,
  lastAction,
}: {
  control: SimulationControl | null
  onControl: (action: string) => void
  onCrisis: (type: string, durationTicks: number, parameters: Record<string, unknown>) => void
  busy: boolean
  error: string | null
  lastAction: string | null
}) {
  const [duration, setDuration] = useState(8)
  const [crisisType, setCrisisType] = useState(CRISES[0].type)

  const running = control?.status === 'RUNNING'

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Simulation</h1>
          <p className="view-subtitle">
            The organizer-provided simulator's controls. These change the simulated world
            and nothing else.
          </p>
        </div>
      </div>

      <div className="banner banner-info" role="note">
        <span className="banner-tag">SIMULATION ONLY</span>
        <span>
          These controls act on the simulator. They do not ship fuel — only an approved
          recommendation does that.
        </span>
      </div>

      <div className="grid grid-2">
        <Panel
          title="Simulation controls"
          subtitle="Run, pause, step and reset the simulated world"
        >
          {control ? (
            <>
              <dl className="definition-list">
                <dt>Status</dt>
                <dd>
                  <StatusPill status={control.status} />
                </dd>
                <dt>Tick</dt>
                <dd>{control.tick ?? '—'}</dd>
                <dt>Simulated time</dt>
                <dd>{simTime(control.sim_time)}</dd>
                <dt>Tick length</dt>
                <dd>{control.tick_minutes ? `${control.tick_minutes} minutes` : '—'}</dd>
                <dt>Scenario</dt>
                <dd>{control.scenario_id ?? '—'}</dd>
              </dl>

              <div className="button-row">
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => onControl(running ? 'pause' : 'run')}
                  disabled={busy}
                  data-testid="simulation-run-pause"
                >
                  {running ? 'Pause simulation' : 'Run simulation'}
                </button>
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => onControl('step')}
                  disabled={busy}
                >
                  Step one tick
                </button>
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => onControl('toggle')}
                  disabled={busy}
                >
                  Toggle
                </button>
                <button
                  type="button"
                  className="button button-danger"
                  onClick={() => onControl('reset')}
                  disabled={busy}
                >
                  Reset world
                </button>
              </div>

              {lastAction ? <p className="muted">Last action: {lastAction}</p> : null}
              {error ? (
                <p className="rec-error" role="alert">
                  {error}
                </p>
              ) : null}
            </>
          ) : (
            <EmptyState message="The simulation endpoint is not answering." />
          )}
        </Panel>

        <Panel
          title="Crisis injection"
          subtitle="Inject a crisis into the simulated world"
          actions={<span className="pill status-warn">simulation only</span>}
        >
          <label className="field">
            <span className="field-label">Crisis</span>
            <select
              className="input"
              value={crisisType}
              onChange={(event) => setCrisisType(event.target.value)}
              data-testid="crisis-type"
            >
              {CRISES.map((crisis) => (
                <option key={crisis.type} value={crisis.type}>
                  {crisis.label}
                </option>
              ))}
            </select>
          </label>

          <p className="muted">
            {CRISES.find((crisis) => crisis.type === crisisType)?.hint}
          </p>

          <label className="field">
            <span className="field-label">Duration (ticks)</span>
            <input
              className="input"
              type="number"
              min={1}
              max={200}
              value={duration}
              onChange={(event) => setDuration(Number(event.target.value))}
              data-testid="crisis-duration"
            />
          </label>

          <div className="button-row">
            <button
              type="button"
              className="button button-danger"
              disabled={busy}
              onClick={() => {
                const crisis = CRISES.find((item) => item.type === crisisType)
                onCrisis(crisisType, duration, crisis?.parameters ?? {})
              }}
              data-testid="inject-crisis"
            >
              Inject crisis
            </button>
          </div>

          <p className="muted">
            The crisis is injected into the simulator only. The platform reacts to it the
            same way it reacts to any other change in the world.
          </p>
        </Panel>
      </div>

      <Panel title="What each crisis does" subtitle="The simulator's six injectable events">
        <table className="table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Effect</th>
              <th>Parameters</th>
            </tr>
          </thead>
          <tbody>
            {CRISES.map((crisis) => (
              <tr key={crisis.type}>
                <td>
                  <code className="code-block">{crisis.type}</code>
                </td>
                <td>{crisis.hint}</td>
                <td className="muted">
                  {Object.entries(crisis.parameters)
                    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
                    .join(', ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  )
}
