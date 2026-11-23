/**
 * The application shell.
 *
 * A left-hand navigation, a top bar carrying the simulation's clock and
 * the platform's health, and the current view. The health banner is
 * always visible: an operator has to be able to see, at a glance, that
 * the platform is degraded or that what they are looking at is stale.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { age, simTime } from '../format'
import type { Freshness, HealthReport, ServiceHealth, StateSnapshot } from '../types'
import { StaleBanner } from './ui'

export type ViewId =
  | 'overview'
  | 'network'
  | 'inventory'
  | 'risk'
  | 'recommendations'
  | 'timeline'
  | 'history'
  | 'health'
  | 'simulation'

export interface NavItem {
  id: ViewId
  label: string
  hint: string
}

export const NAV_ITEMS: NavItem[] = [
  { id: 'overview', label: 'Overview', hint: 'Tick, service level, alerts' },
  { id: 'network', label: 'Network', hint: 'Depots, stations, routes' },
  { id: 'inventory', label: 'Inventory', hint: 'Stock by station and fuel' },
  { id: 'risk', label: 'Risk & alerts', hint: 'Shortage risk, with explanations' },
  { id: 'recommendations', label: 'Recommendations', hint: 'Approve or reject' },
  { id: 'timeline', label: 'Supply & events', hint: 'Arrivals and crises' },
  { id: 'history', label: 'Decision history', hint: 'What was decided, and what happened' },
  { id: 'health', label: 'Service health', hint: 'Every dependency' },
  { id: 'simulation', label: 'Simulation', hint: 'Run, pause, inject crises' },
]

export function HealthBanner({
  state,
  health,
  serviceHealth,
}: {
  state: StateSnapshot | null
  health: HealthReport | null
  serviceHealth: ServiceHealth | null
}) {
  const freshness: Freshness | null = state
  const degraded = Boolean(freshness?.degraded || health?.status === 'degraded')
  const stale = Boolean(freshness?.stale)

  if (degraded || stale) {
    return (
      <StaleBanner
        degraded={degraded}
        stale={stale}
        ageSeconds={freshness?.age_seconds ?? null}
        reason={freshness?.degraded_reason ?? null}
      />
    )
  }

  const fallbackActive = serviceHealth?.components.decision_engine.fallback_active ?? false
  if (fallbackActive) {
    return (
      <div className="banner banner-warn" role="status">
        <span className="banner-tag">FALLBACK</span>
        <span>
          The prediction service is unavailable, so the platform is using the rule-based
          policy — refilling the lowest days-of-cover first.
        </span>
      </div>
    )
  }

  return null
}

export function AppShell({
  view,
  onNavigate,
  state,
  health,
  serviceHealth,
  children,
}: {
  view: ViewId
  onNavigate: (view: ViewId) => void
  state: StateSnapshot | null
  health: HealthReport | null
  serviceHealth: ServiceHealth | null
  children: ReactNode
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const criticalCount = serviceHealth?.components.decision_engine.counters?.failed ?? 0

  return (
    <div className="shell">
      <aside className={`sidebar ${menuOpen ? 'sidebar-open' : ''}`}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ⛽
          </span>
          <div>
            <strong>Fuel Platform</strong>
            <small>BUP CSE Fest 2026</small>
          </div>
        </div>
        <nav aria-label="Dashboard sections">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-item ${view === item.id ? 'nav-item-active' : ''}`.trim()}
              onClick={() => {
                onNavigate(item.id)
                setMenuOpen(false)
              }}
              aria-current={view === item.id ? 'page' : undefined}
            >
              <span className="nav-label">{item.label}</span>
              <span className="nav-hint">{item.hint}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className="muted">Day 5 · operator dashboard</span>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            type="button"
            className="menu-toggle"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label="Toggle navigation"
            aria-expanded={menuOpen}
          >
            ☰
          </button>
          <div className="topbar-status">
            <div className="topbar-item">
              <span className="topbar-label">Tick</span>
              <span className="topbar-value">{state?.instance.tick ?? '—'}</span>
            </div>
            <div className="topbar-item">
              <span className="topbar-label">Sim time</span>
              <span className="topbar-value">{simTime(state?.instance.sim_time)}</span>
            </div>
            <div className="topbar-item">
              <span className="topbar-label">Simulation</span>
              <span className={`topbar-value status-${(state?.instance.status ?? 'unknown').toLowerCase()}`}>
                {state?.instance.status ?? '—'}
              </span>
            </div>
            <div className="topbar-item">
              <span className="topbar-label">Service level</span>
              <span className="topbar-value">
                {state ? `${(state.metrics.service_level * 100).toFixed(2)}%` : '—'}
              </span>
            </div>
            <div className="topbar-item">
              <span className="topbar-label">Last sync</span>
              <span className="topbar-value">{age(state?.age_seconds)}</span>
            </div>
            {criticalCount > 0 ? (
              <div className="topbar-item">
                <span className="topbar-label">Failed decisions</span>
                <span className="topbar-value status-bad">{criticalCount}</span>
              </div>
            ) : null}
          </div>
        </header>

        <HealthBanner state={state} health={health} serviceHealth={serviceHealth} />

        <main className="content">{children}</main>
      </div>
    </div>
  )
}
