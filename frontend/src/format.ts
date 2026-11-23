/**
 * The shared formatting helpers.
 *
 * Everything the operator reads goes through one of these, so a number
 * looks the same on every screen. The units are the simulator's: litres
 * for fuel, simulated hours for cover.
 */

import type { RiskLevel } from './types'

export function litres(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value).toLocaleString()} L`
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function hours(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (!Number.isFinite(value)) return '∞'
  if (value >= 1000) return '∞'
  return `${value.toFixed(1)} h`
}

export function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString(undefined, { maximumFractionDigits: digits })
}

/** The severity class for a risk band. */
export function riskClass(level: RiskLevel | string): string {
  switch (level) {
    case 'CRITICAL':
      return 'risk-critical'
    case 'WATCH':
      return 'risk-watch'
    default:
      return 'risk-ok'
  }
}

/** The severity class for a station, depot or route status. */
export function statusClass(status: string): string {
  switch (status) {
    case 'OUTAGE':
    case 'DISRUPTED':
    case 'FAILED':
      return 'status-bad'
    case 'CONSTRAINED':
    case 'DELAYED':
    case 'WATCH':
      return 'status-warn'
    case 'CANCELLED':
      return 'status-muted'
    default:
      return 'status-good'
  }
}

/** How long ago something happened, in plain words. */
export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'unknown'
  if (seconds < 60) return `${Math.round(seconds)} s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return `${(seconds / 3600).toFixed(1)} h ago`
}

/** A short, readable form of a sim timestamp. */
export function simTime(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toISOString().replace('T', ' ').slice(0, 19) + 'Z'
}

/** The fill fraction of a tank, clamped to 0..1. */
export function fillFraction(
  onHand: number | undefined,
  capacity: number | undefined,
): number {
  if (!capacity || capacity <= 0) return 0
  if (!onHand) return 0
  return Math.max(0, Math.min(1, onHand / capacity))
}
