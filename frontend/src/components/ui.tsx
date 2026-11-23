/**
 * The shared presentational pieces.
 *
 * Every view uses these, so a loading state looks the same everywhere and
 * an error is always reported the same way.
 */

import type { ReactNode } from 'react'

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = '',
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`panel ${className}`.trim()}>
      <header className="panel-header">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p className="panel-subtitle">{subtitle}</p> : null}
        </div>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string
  onRetry?: () => void
}) {
  return (
    <div className="state state-error" role="alert">
      <strong>Something went wrong.</strong>
      <span>{message}</span>
      {onRetry ? (
        <button type="button" className="button button-ghost" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  )
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="state state-empty">
      <span>{message}</span>
    </div>
  )
}

/**
 * The banner shown when the data on screen is older than the world.
 *
 * The point is that the operator is never quietly shown stale data: the
 * age is stated, and the banner says what is being displayed.
 */
export function StaleBanner({
  degraded,
  stale,
  ageSeconds,
  reason,
}: {
  degraded: boolean
  stale: boolean
  ageSeconds: number | null
  reason: string | null
}) {
  if (!degraded && !stale) return null

  return (
    <div className="banner banner-warn" role="status">
      <span className="banner-tag">DEGRADED</span>
      <span>
        Showing cached data
        {ageSeconds !== null ? ` from ${Math.round(ageSeconds)} s ago` : ''}.
        {reason ? ` ${reason}` : ' The simulator is not answering right now.'}
      </span>
    </div>
  )
}

export function StatusPill({
  status,
  className = '',
}: {
  status: string
  className?: string
}) {
  return (
    <span className={`pill ${className}`.trim()} data-status={status}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

export function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint?: string
  tone?: 'good' | 'warn' | 'bad'
}) {
  return (
    <div className={`metric ${tone ? `metric-${tone}` : ''}`.trim()}>
      <span className="metric-label">{label}</span>
      <span className="metric-value">{value}</span>
      {hint ? <span className="metric-hint">{hint}</span> : null}
    </div>
  )
}

export function FillBar({
  fraction,
  label,
  tone = 'good',
}: {
  fraction: number
  label: string
  tone?: 'good' | 'warn' | 'bad'
}) {
  const clamped = Math.max(0, Math.min(1, fraction))
  return (
    <div
      className="fill-bar"
      role="meter"
      aria-valuenow={Math.round(clamped * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div className={`fill-bar-fill fill-${tone}`} style={{ width: `${clamped * 100}%` }} />
    </div>
  )
}

export function EmptyOr({
  condition,
  whenEmpty,
  children,
}: {
  condition: boolean
  whenEmpty: ReactNode
  children: ReactNode
}) {
  return <>{condition ? whenEmpty : children}</>
}
