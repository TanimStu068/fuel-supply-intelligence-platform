/**
 * The dashboard.
 *
 * One polling loop feeds the whole app: the state, the risk assessments,
 * the alerts, the recommendations, the decision history and the service
 * health are all read from the same backend on the same cadence, so the
 * screens cannot disagree with each other.
 *
 * The approve / reject / what-if actions are deliberately *not* part of
 * the poll: they write, so they run once, report their own error, and
 * then trigger a refresh.
 */

import { useCallback, useMemo, useState } from 'react'
import {
  approveRecommendation,
  controlSimulation,
  fetchAlerts,
  fetchDecisionHistory,
  fetchDecisionSettings,
  fetchForecasts,
  fetchHealth,
  fetchRecommendations,
  fetchRisks,
  fetchServiceHealth,
  fetchSimulation,
  fetchState,
  injectCrisis,
  proposeRecommendations,
  rejectRecommendation,
  simulateRecommendation,
} from './api'
import { AppShell, type ViewId } from './components/AppShell'
import { ErrorState, Loading } from './components/ui'
import { usePolled } from './hooks/usePolled'
import { HealthView } from './views/HealthView'
import { HistoryView } from './views/HistoryView'
import { InventoryView } from './views/InventoryView'
import { NetworkView } from './views/NetworkView'
import { OverviewView } from './views/OverviewView'
import { RecommendationsView } from './views/RecommendationsView'
import { RiskView } from './views/RiskView'
import { SimulationView } from './views/SimulationView'
import { TimelineView } from './views/TimelineView'
import type { SimulationResult } from './types'

const POLL_MS = 5000

export default function App() {
  const [view, setView] = useState<ViewId>('overview')
  const [simulations, setSimulations] = useState<Record<string, SimulationResult | null>>({})
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  const [actionErrors, setActionErrors] = useState<Record<string, string | null>>({})
  const [simulationBusy, setSimulationBusy] = useState(false)
  const [simulationError, setSimulationError] = useState<string | null>(null)
  const [lastAction, setLastAction] = useState<string | null>(null)

  const health = usePolled(fetchHealth, POLL_MS * 2)
  const state = usePolled(fetchState, POLL_MS)
  const risks = usePolled(fetchRisks, POLL_MS)
  const forecasts = usePolled(fetchForecasts, POLL_MS * 2)
  const alerts = usePolled(fetchAlerts, POLL_MS)
  const recommendations = usePolled(fetchRecommendations, POLL_MS)
  const history = usePolled(fetchDecisionHistory, POLL_MS)
  const settings = usePolled(fetchDecisionSettings, POLL_MS)
  const serviceHealth = usePolled(fetchServiceHealth, POLL_MS * 2)
  const simulation = usePolled(fetchSimulation, POLL_MS)

  const refreshAll = useCallback(() => {
    state.refresh()
    risks.refresh()
    recommendations.refresh()
    history.refresh()
    alerts.refresh()
  }, [state, risks, recommendations, history, alerts])

  const setBusy = useCallback((id: string, busy: boolean) => {
    setBusyIds((current) => {
      const next = new Set(current)
      if (busy) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])

  const handleApprove = useCallback(
    async (id: string) => {
      setBusy(id, true)
      setActionErrors((current) => ({ ...current, [id]: null }))
      try {
        await approveRecommendation(id, 'operator')
        refreshAll()
      } catch (cause) {
        setActionErrors((current) => ({
          ...current,
          [id]: cause instanceof Error ? cause.message : String(cause),
        }))
      } finally {
        setBusy(id, false)
      }
    },
    [setBusy, refreshAll],
  )

  const handleReject = useCallback(
    async (id: string) => {
      setBusy(id, true)
      setActionErrors((current) => ({ ...current, [id]: null }))
      try {
        await rejectRecommendation(id, 'operator', 'rejected from the dashboard')
        refreshAll()
      } catch (cause) {
        setActionErrors((current) => ({
          ...current,
          [id]: cause instanceof Error ? cause.message : String(cause),
        }))
      } finally {
        setBusy(id, false)
      }
    },
    [setBusy, refreshAll],
  )

  const handleSimulate = useCallback(
    async (id: string) => {
      setBusy(id, true)
      setActionErrors((current) => ({ ...current, [id]: null }))
      try {
        const result = await simulateRecommendation(id)
        setSimulations((current) => ({ ...current, [id]: result }))
      } catch (cause) {
        setActionErrors((current) => ({
          ...current,
          [id]: cause instanceof Error ? cause.message : String(cause),
        }))
      } finally {
        setBusy(id, false)
      }
    },
    [setBusy],
  )

  const handlePropose = useCallback(async () => {
    try {
      await proposeRecommendations()
      refreshAll()
    } catch (cause) {
      setSimulationError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [refreshAll])

  const handleSimulationControl = useCallback(
    async (action: string) => {
      setSimulationBusy(true)
      setSimulationError(null)
      try {
        await controlSimulation(action)
        setLastAction(action)
        simulation.refresh()
        refreshAll()
      } catch (cause) {
        setSimulationError(cause instanceof Error ? cause.message : String(cause))
      } finally {
        setSimulationBusy(false)
      }
    },
    [simulation, refreshAll],
  )

  const handleCrisis = useCallback(
    async (type: string, durationTicks: number, parameters: Record<string, unknown>) => {
      setSimulationBusy(true)
      setSimulationError(null)
      try {
        await injectCrisis(type, durationTicks, parameters)
        setLastAction(`crisis: ${type}`)
        simulation.refresh()
        refreshAll()
      } catch (cause) {
        setSimulationError(cause instanceof Error ? cause.message : String(cause))
      } finally {
        setSimulationBusy(false)
      }
    },
    [simulation, refreshAll],
  )

  const riskList = risks.data?.risks ?? []
  const alertList = alerts.data?.alerts ?? []
  const forecastList = forecasts.data?.forecasts ?? []
  const recommendationList = recommendations.data?.recommendations ?? []

  const body = useMemo(() => {
    switch (view) {
      case 'overview':
        return (
          <OverviewView
            state={state.data}
            risks={riskList}
            alerts={alertList}
            recommendations={recommendationList}
            serviceHealth={serviceHealth.data}
          />
        )
      case 'network':
        return (
          <NetworkView
            depots={state.data?.depots ?? []}
            stations={state.data?.stations ?? []}
            routes={state.data?.routes ?? []}
            regions={state.data?.regions ?? []}
          />
        )
      case 'inventory':
        return (
          <InventoryView
            stations={state.data?.stations ?? []}
            depots={state.data?.depots ?? []}
            risks={riskList}
          />
        )
      case 'risk':
        return <RiskView risks={riskList} alerts={alertList} forecasts={forecastList} />
      case 'recommendations':
        return (
          <RecommendationsView
            recommendations={recommendationList}
            autoMode={settings.data?.auto_mode ?? false}
            fallbackReason={recommendations.data?.fallback_reason ?? null}
            onApprove={handleApprove}
            onReject={handleReject}
            onSimulate={handleSimulate}
            onPropose={handlePropose}
            simulations={simulations}
            busyIds={busyIds}
            actionErrors={actionErrors}
          />
        )
      case 'timeline':
        return (
          <TimelineView
            arrivals={state.data?.supply_arrivals ?? []}
            events={state.data?.events ?? []}
          />
        )
      case 'history':
        return <HistoryView decisions={history.data?.decisions ?? []} />
      case 'health':
        return <HealthView health={serviceHealth.data} />
      case 'simulation':
        return (
          <SimulationView
            control={simulation.data}
            onControl={handleSimulationControl}
            onCrisis={handleCrisis}
            busy={simulationBusy}
            error={simulationError}
            lastAction={lastAction}
          />
        )
      default:
        return null
    }
  }, [
    view,
    state.data,
    riskList,
    alertList,
    forecastList,
    recommendationList,
    settings.data?.auto_mode,
    recommendations.data?.fallback_reason,
    serviceHealth.data,
    history.data?.decisions,
    simulation.data,
    simulations,
    busyIds,
    actionErrors,
    handleApprove,
    handleReject,
    handleSimulate,
    handlePropose,
    handleSimulationControl,
    handleCrisis,
    simulationBusy,
    simulationError,
    lastAction,
  ])

  if (state.loading && !state.data) {
    return (
      <div className="app">
        <Loading label="Connecting to the platform…" />
      </div>
    )
  }

  if (!state.data && state.error) {
    return (
      <div className="app">
        <h1>Fuel Supply Intelligence &amp; Resilience Platform</h1>
        <ErrorState message={state.error} onRetry={state.refresh} />
      </div>
    )
  }

  return (
    <AppShell
      view={view}
      onNavigate={setView}
      state={state.data}
      health={health.data}
      serviceHealth={serviceHealth.data}
    >
      {body}
    </AppShell>
  )
}
