/**
 * The typed API client.
 *
 * Every call goes through `request`, so the error handling is in one
 * place: a failed call becomes an `ApiError` carrying the status code
 * and the server's own message, which is what the UI shows the operator.
 */

import { API_BASE_URL } from './config'
import type {
  AlertReport,
  DecisionHistoryReport,
  DecisionSettings,
  ForecastReport,
  HealthReport,
  Recommendation,
  RecommendationReport,
  RiskReport,
  ServiceHealth,
  SimulationControl,
  SimulationResult,
  StateSnapshot,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string, path: string) {
    super(detail || `${path} failed with HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
      ...init,
    })
  } catch (cause) {
    // A network failure is not an HTTP status. It still has to reach the
    // operator as a readable message rather than an unhandled rejection.
    throw new ApiError(0, `the backend is unreachable: ${String(cause)}`, path)
  }

  if (!response.ok) {
    let detail = ''
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') {
        detail = body.detail
      } else if (body.detail) {
        detail = JSON.stringify(body.detail)
      }
    } catch {
      detail = await response.text().catch(() => '')
    }
    throw new ApiError(response.status, detail, path)
  }

  return (await response.json()) as T
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export const fetchHealth = () => request<HealthReport>('/health')
export const fetchState = () => request<StateSnapshot>('/api/state')
export const fetchRisks = () => request<RiskReport>('/api/risk')
export const fetchForecasts = () => request<ForecastReport>('/api/forecast')
export const fetchAlerts = () => request<AlertReport>('/api/alerts')
export const fetchRecommendations = () => request<RecommendationReport>('/api/recommendations')
export const fetchDecisionHistory = () => request<DecisionHistoryReport>('/api/decision-history')
export const fetchDecisionSettings = () => request<DecisionSettings>('/api/decision-settings')
export const fetchServiceHealth = () => request<ServiceHealth>('/api/service-health')
export const fetchSimulation = () => request<SimulationControl>('/api/simulation')

export const proposeRecommendations = () =>
  post<{ count: number; used_fallback_policy: boolean; recommendations: Recommendation[] }>(
    '/api/recommendations/propose',
  )

export const simulateRecommendation = (id: string) =>
  post<SimulationResult>(`/api/recommendations/${encodeURIComponent(id)}/simulate`)

export const approveRecommendation = (id: string, decidedBy: string) =>
  post<Recommendation>(`/api/recommendations/${encodeURIComponent(id)}/approve`, {
    decided_by: decidedBy,
    submit: true,
  })

export const rejectRecommendation = (id: string, decidedBy: string, reason: string) =>
  post<Recommendation>(`/api/recommendations/${encodeURIComponent(id)}/reject`, {
    decided_by: decidedBy,
    reason,
  })

export const setAutoMode = (autoMode: boolean) =>
  post<DecisionSettings>('/api/decision-settings', { auto_mode: autoMode })

export const controlSimulation = (action: string) =>
  post<{ simulation_only: boolean; action: string; result: Record<string, unknown> }>(
    `/api/simulation/${action}`,
  )

export const injectCrisis = (type: string, durationTicks: number, parameters: Record<string, unknown>) =>
  post<{ simulation_only: boolean; event: Record<string, unknown> }>('/api/simulation/crisis', {
    type,
    duration_ticks: durationTicks,
    parameters,
  })
