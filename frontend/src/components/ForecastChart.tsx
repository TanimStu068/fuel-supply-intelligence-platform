/**
 * The forecast chart.
 *
 * A plain SVG line with a shaded confidence band. It is drawn by hand
 * rather than pulled in from a charting library for two reasons: the band
 * and the line have to share the same scale, and the whole chart has to
 * fit in a drawer without a second scrollbar.
 */

import { useMemo } from 'react'
import type { ForecastPoint } from '../types'

const WIDTH = 640
const HEIGHT = 180
const PADDING = { top: 12, right: 12, bottom: 24, left: 44 }

export function ForecastChart({
  points,
  title,
}: {
  points: ForecastPoint[]
  title: string
}) {
  const geometry = useMemo(() => {
    if (points.length === 0) return null

    const maxValue = Math.max(
      ...points.map((point) => Math.max(point.upper_bound, point.predicted_demand)),
      1,
    )
    const innerWidth = WIDTH - PADDING.left - PADDING.right
    const innerHeight = HEIGHT - PADDING.top - PADDING.bottom
    const lastIndex = Math.max(1, points.length - 1)

    const x = (index: number) => PADDING.left + (index / lastIndex) * innerWidth
    const y = (value: number) => PADDING.top + innerHeight - (value / maxValue) * innerHeight

    const line = points
      .map((point, index) => `${index === 0 ? 'M' : 'L'} ${x(index).toFixed(2)} ${y(point.predicted_demand).toFixed(2)}`)
      .join(' ')

    const band = points
      .map((point, index) => `${index === 0 ? 'M' : 'L'} ${x(index).toFixed(2)} ${y(point.upper_bound).toFixed(2)}`)
      .join(' ') +
      ' ' +
      [...points]
        .reverse()
        .map((point, index) => {
          const original = points.length - 1 - index
          return `L ${x(original).toFixed(2)} ${y(point.lower_bound).toFixed(2)}`
        })
        .join(' ') +
      ' Z'

    return { line, band, x, y, maxValue, innerHeight }
  }, [points])

  if (!geometry) {
    return <p className="muted">No forecast points for this station and fuel.</p>
  }

  const { line, band, maxValue } = geometry
  const ticks = [0, 0.25, 0.5, 0.75, 1]

  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="chart-svg"
        role="img"
        aria-label={`${title}: forecast demand with confidence band`}
        preserveAspectRatio="xMidYMid meet"
      >
        {ticks.map((tick) => {
          const value = maxValue * tick
          const y = geometry.y(value)
          return (
            <g key={tick}>
              <line
                x1={PADDING.left}
                x2={WIDTH - PADDING.right}
                y1={y}
                y2={y}
                className="chart-grid"
              />
              <text x={PADDING.left - 6} y={y + 3} className="chart-axis" textAnchor="end">
                {Math.round(value)}
              </text>
            </g>
          )
        })}
        <path d={band} className="chart-band" />
        <path d={line} className="chart-line" />
      </svg>
      <p className="chart-legend">
        <span className="legend-swatch legend-line" /> forecast demand
        <span className="legend-swatch legend-band" /> confidence band (lower–upper)
      </p>
    </figure>
  )
}
