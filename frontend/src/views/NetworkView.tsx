/**
 * The network board.
 *
 * The network as a picture: two depots on the left, four stations on the
 * right, and the six routes between them. The colours are the *live*
 * statuses — a disrupted route is drawn broken, a station in outage is
 * drawn hollow, a constrained depot is drawn amber.
 *
 * The layout is fixed rather than force-directed: the topology is known
 * and small, so a stable picture is more readable than one that moves
 * every time a fuel level changes.
 */

import { litres, statusClass } from '../format'
import type { Depot, Region, Route, Station } from '../types'
import { Panel, StatusPill } from '../components/ui'

/** Where each entity sits on the board, in board coordinates. */
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  'depot-gazipur': { x: 110, y: 130 },
  'depot-patiya': { x: 110, y: 330 },
  'station-mirpur': { x: 500, y: 80 },
  'station-tongi': { x: 500, y: 200 },
  'station-coxsbazar': { x: 500, y: 320 },
  'station-karnaphuli': { x: 500, y: 440 },
}

const FALLBACK_POSITIONS = [
  { x: 120, y: 90 },
  { x: 120, y: 210 },
  { x: 120, y: 330 },
  { x: 520, y: 80 },
  { x: 520, y: 180 },
  { x: 520, y: 280 },
  { x: 520, y: 380 },
]

const WIDTH = 660
const HEIGHT = 540

/** How far a node's caption sits below the node itself. */
const CAPTION_OFFSET = 40

export function NetworkView({
  depots,
  stations,
  routes,
  regions,
}: {
  depots: Depot[]
  stations: Station[]
  routes: Route[]
  regions: Region[]
}) {
  const positions = new Map<string, { x: number; y: number }>()
  let index = 0
  for (const id of [...depots.map((d) => d.id), ...stations.map((s) => s.id)]) {
    positions.set(
      id,
      NODE_POSITIONS[id] ?? FALLBACK_POSITIONS[index % FALLBACK_POSITIONS.length],
    )
    index += 1
  }

  const depotById = new Map(depots.map((depot) => [depot.id, depot]))
  const stationById = new Map(stations.map((station) => [station.id, station]))

  return (
    <div className="view">
      <div className="view-header">
        <div>
          <h1>Network</h1>
          <p className="view-subtitle">
            {depots.length} depots, {stations.length} stations, {routes.length} routes across{' '}
            {regions.length} regions. Colours are live.
          </p>
        </div>
      </div>

      <Panel
        title="Supply topology"
        subtitle="Depots ship to stations over routes; a broken route is drawn broken"
      >
        <div className="board-wrap">
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            className="board"
            role="img"
            aria-label="The fuel supply network: depots, stations and the routes between them"
          >
            <defs>
              <pattern id="disrupted" width="8" height="8" patternUnits="userSpaceOnUse">
                <path d="M0 8 L8 0" className="board-hatch" strokeWidth="3" />
              </pattern>
            </defs>

            {regions.map((region, regionIndex) => (
              <g key={region.id}>
                <rect
                  x={regionIndex * 30}
                  y={regionIndex * 34}
                  width={WIDTH - 40 - regionIndex * 30}
                  height={HEIGHT - 40 - regionIndex * 34}
                  rx="12"
                  className="board-region"
                />
                <text
                  x={18 + regionIndex * 30}
                  y={24 + regionIndex * 34}
                  className="board-region-label"
                >
                  {region.name}
                </text>
              </g>
            ))}

            {routes.map((route) => {
              const from = positions.get(route.source_depot_id)
              const to = positions.get(route.destination_station_id)
              if (!from || !to) return null
              const disrupted = route.status === 'DISRUPTED'
              return (
                <g key={route.id} className="board-route">
                  <line
                    x1={from.x + 38}
                    y1={from.y}
                    x2={to.x - 38}
                    y2={to.y}
                    className={`board-edge ${disrupted ? 'board-edge-disrupted' : 'board-edge-open'}`}
                    strokeDasharray={disrupted ? '6 5' : undefined}
                  />
                  {disrupted ? (
                    <line
                      x1={from.x + 38}
                      y1={from.y}
                      x2={to.x - 38}
                      y2={to.y}
                      className="board-edge-strike"
                    />
                  ) : null}
                  <text
                    x={(from.x + to.x) / 2}
                    y={(from.y + to.y) / 2 - 6}
                    className="board-edge-label"
                    textAnchor="middle"
                  >
                    {route.transit_ticks}t
                  </text>
                </g>
              )
            })}

            {depots.map((depot) => {
              const position = positions.get(depot.id)
              if (!position) return null
              const constrained = depot.status === 'CONSTRAINED'
              return (
                <g key={depot.id} className="board-node">
                  <rect
                    x={position.x - 34}
                    y={position.y - 16}
                    width={68}
                    height={32}
                    rx={6}
                    className={`board-shape ${constrained ? 'board-shape-warn' : 'board-shape-depot'}`}
                  />
                  <text x={position.x} y={position.y + 4} className="board-node-sub" textAnchor="middle">
                    {depot.status}
                  </text>
                  <text
                    x={position.x}
                    y={position.y + CAPTION_OFFSET - 12}
                    className="board-node-label"
                    textAnchor="middle"
                  >
                    {depot.name}
                  </text>
                  <text
                    x={position.x}
                    y={position.y + CAPTION_OFFSET + 2}
                    className="board-caption"
                    textAnchor="middle"
                  >
                    {depot.id}
                  </text>
                </g>
              )
            })}

            {stations.map((station) => {
              const position = positions.get(station.id)
              if (!position) return null
              const outage = station.status === 'OUTAGE'
              const total = Object.values(station.inventory).reduce((sum, value) => sum + value, 0)
              const capacity = Object.values(station.capacity).reduce((sum, value) => sum + value, 0)
              const fill = capacity > 0 ? total / capacity : 0
              return (
                <g key={station.id} className="board-node">
                  <circle
                    cx={position.x}
                    cy={position.y}
                    r={24}
                    className={`board-shape ${outage ? 'board-shape-outage' : 'board-shape-station'}`}
                  />
                  {outage ? <line x1={position.x - 16} y1={position.y - 16} x2={position.x + 16} y2={position.y + 16} className="board-strike" /> : null}
                  <text x={position.x} y={position.y - 2} className="board-node-label" textAnchor="middle">
                    {Math.round(fill * 100)}%
                  </text>
                  <text
                    x={position.x}
                    y={position.y + CAPTION_OFFSET - 12}
                    className="board-node-label"
                    textAnchor="middle"
                  >
                    {station.name}
                  </text>
                  <text
                    x={position.x}
                    y={position.y + CAPTION_OFFSET + 2}
                    className="board-caption"
                    textAnchor="middle"
                  >
                    {station.id}
                  </text>
                </g>
              )
            })}
          </svg>
        </div>

        <div className="legend">
          <span className="legend-item">
            <span className="legend-swatch legend-good" /> open / available
          </span>
          <span className="legend-item">
            <span className="legend-swatch legend-warn" /> constrained / delayed
          </span>
          <span className="legend-item">
            <span className="legend-swatch legend-bad" /> disrupted / outage / failed
          </span>
        </div>
      </Panel>

      <div className="grid grid-2">
        <Panel title="Depots" subtitle="Stock and dispatch allowance">
          <table className="table">
            <thead>
              <tr>
                <th>Depot</th>
                <th>Status</th>
                <th>Dispatch / tick</th>
                <th>Stock</th>
              </tr>
            </thead>
            <tbody>
              {depots.map((depot) => (
                <tr key={depot.id}>
                  <td>{depot.name}</td>
                  <td>
                    <StatusPill status={depot.status} className={statusClass(depot.status)} />
                  </td>
                  <td>{litres(depot.dispatch_capacity_per_tick)}</td>
                  <td>{litres(Object.values(depot.inventory).reduce((s, v) => s + v, 0))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Stations" subtitle="Status and stock">
          <table className="table">
            <thead>
              <tr>
                <th>Station</th>
                <th>Status</th>
                <th>Profile</th>
                <th>Stock</th>
              </tr>
            </thead>
            <tbody>
              {stations.map((station) => (
                <tr key={station.id}>
                  <td>{station.name}</td>
                  <td>
                    <StatusPill status={station.status} className={statusClass(station.status)} />
                  </td>
                  <td>{station.demand_profile}</td>
                  <td>
                    {litres(Object.values(station.inventory).reduce((s, v) => s + v, 0))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <Panel title="Routes" subtitle="Six routes between the depots and the stations">
        <table className="table">
          <thead>
            <tr>
              <th>Route</th>
              <th>From</th>
              <th>To</th>
              <th>Transit</th>
              <th>Max shipment</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {routes.map((route) => (
              <tr key={route.id}>
                <td>{route.id}</td>
                <td>{depotById.get(route.source_depot_id)?.name ?? route.source_depot_id}</td>
                <td>{stationById.get(route.destination_station_id)?.name ?? route.destination_station_id}</td>
                <td>{route.transit_ticks} ticks</td>
                <td>{litres(route.max_shipment)}</td>
                <td>
                  <StatusPill status={route.status} className={statusClass(route.status)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  )
}
