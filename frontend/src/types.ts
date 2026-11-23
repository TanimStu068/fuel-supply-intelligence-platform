/**
 * The shapes the backend serves.
 *
 * These mirror `backend/app/simulator_models.py` and the payloads the
 * decision and intelligence layers publish. Every optional field is
 * optional here too, because the API says `null` rather than omitting a
 * key in several places.
 */

export type FuelType = 'DIESEL' | 'PETROL' | 'OCTANE'

export const FUEL_TYPES: FuelType[] = ['DIESEL', 'PETROL', 'OCTANE']

export type RiskLevel = 'OK' | 'WATCH' | 'CRITICAL'

export type RecommendationStatus =
  | 'PROPOSED'
  | 'APPROVED'
  | 'REJECTED'
  | 'SUBMITTED'
  | 'ARRIVED'
  | 'FAILED'

export interface FuelMap {
  DIESEL: number
  PETROL: number
  OCTANE: number
}

export interface Instance {
  id: number
  scenario_id: string
  scenario_version: string
  seed: number
  sim_time: string
  tick: number
  tick_minutes: number
  status: 'PAUSED' | 'RUNNING'
}

export interface Region {
  id: string
  name: string
  demand_factor: number
}

export interface Depot {
  id: string
  name: string
  region_id: string
  status: 'OPEN' | 'CONSTRAINED'
  dispatch_capacity_per_tick: number
  capacity: FuelMap
  inventory: FuelMap
}

export interface Station {
  id: string
  name: string
  region_id: string
  status: 'OPEN' | 'OUTAGE'
  demand_profile: string
  demand_multiplier: number
  capacity: FuelMap
  inventory: FuelMap
}

export interface Route {
  id: string
  source_depot_id: string
  destination_station_id: string
  transit_ticks: number
  max_shipment: number
  status: 'AVAILABLE' | 'DISRUPTED'
}

export interface SupplyArrival {
  id: string
  depot_id: string
  fuel_type: FuelType
  quantity: number
  planned_tick: number
  actual_tick: number | null
  status: 'SCHEDULED' | 'DELAYED' | 'ARRIVED'
}

export interface SimulatorEvent {
  id: number
  type: string
  start_tick: number
  end_tick: number
  status: 'SCHEDULED' | 'ACTIVE' | 'RESOLVED'
  parameters: Record<string, unknown>
}

export interface Allocation {
  id: number
  idempotency_key: string
  source_depot_id: string
  destination_station_id: string
  route_id: string
  fuel_type: FuelType
  quantity: number
  created_tick: number
  departure_tick: number | null
  expected_arrival_tick: number | null
  actual_arrival_tick: number | null
  status: 'PENDING' | 'IN_TRANSIT' | 'ARRIVED' | 'FAILED' | 'CANCELLED'
  failure_reason: string | null
}

export interface Metrics {
  served_demand_liters: number
  unmet_demand_liters: number
  service_level: number
  allocation_liters: number
  allocation_failures: number
}

/** The freshness metadata every state payload carries. */
export interface Freshness {
  stale: boolean
  degraded: boolean
  degraded_reason: string | null
  age_seconds: number | null
  has_state: boolean
  last_sync_age_seconds: number | null
  polling: boolean
  poll_interval_seconds: number
}

export interface StateSnapshot extends Freshness {
  instance: Instance
  regions: Region[]
  depots: Depot[]
  stations: Station[]
  routes: Route[]
  supply_arrivals: SupplyArrival[]
  events: SimulatorEvent[]
  allocations: Allocation[]
  metrics: Metrics
}

export interface HealthReport {
  status: 'ok' | 'degraded'
  service: string
  version: string
  components: {
    backend: { status: string }
    simulator: { status: string; error?: string; simulation?: { status: string; tick: number } }
    database: { status: string; error?: string }
  }
}

// -- intelligence ------------------------------------------------------------

export interface StockSignal {
  name: string
  value: number
  detail: string
}

export interface RiskAssessment {
  station_id: string
  fuel_type: FuelType
  risk_level: RiskLevel
  hours_to_stockout: number | null
  stockout_probability: number
  risk_score: number
  needs_human_review: boolean
  recommendation: string
  model: string
  confidence: number
  forecast_total_liters: number
  forecast_upper_liters: number
  signals: StockSignal[]
}

export interface RiskReport {
  evaluated_at: string | null
  models_used: Record<string, number>
  active_model: string
  needs_human_review_count: number
  fallback_activations: Record<string, number>
  summary: Record<string, number>
  count: number
  risks: RiskAssessment[]
}

export interface ForecastPoint {
  tick: number
  sim_time: string
  predicted_demand: number
  lower_bound: number
  upper_bound: number
  confidence: number
}

export interface Forecast {
  station_id: string
  fuel_type: FuelType
  model: string
  horizon_ticks: number
  confidence: number
  needs_human_review: boolean
  fallback_reason: string | null
  explanation: string
  total_demand: number
  mean_demand_per_tick: number
  lower_total: number
  upper_total: number
  points: ForecastPoint[]
}

export interface ForecastReport {
  evaluated_at: string | null
  models_used: Record<string, number>
  count: number
  forecasts: Forecast[]
}

export interface Alert {
  id: number
  created_at: string
  kind: string
  severity: string
  entity_type: string
  entity_id: string
  sim_tick: number
  message: string
  resolved: boolean
}

export interface AlertReport {
  count: number
  alerts: Alert[]
}

// -- decisions ---------------------------------------------------------------

export interface ConstraintCheck {
  code: string
  satisfied: boolean
  detail: string
  observed: number | null
  limit: number | null
}

export interface Alternative {
  route_id: string
  source_depot_id: string
  transit_ticks: number
  quantity: number
  reason: string
  hours_to_stockout_after: number | null
  score: number
}

export interface RiskProjection {
  risk_level_before: RiskLevel
  risk_level_after: RiskLevel
  hours_to_stockout_before: number | null
  hours_to_stockout_after: number | null
  stockout_probability_before: number
  stockout_probability_after: number
  inventory_before: number
  inventory_after: number
  available_before: number
  available_after: number
  arrival_tick: number
  hours_until_arrival: number
}

export interface Recommendation {
  id: string
  station_id: string
  fuel_type: FuelType
  source_depot_id: string
  route_id: string
  quantity: number
  status: RecommendationStatus
  risk_level: RiskLevel
  hours_to_stockout: number | null
  stockout_probability: number
  projection: RiskProjection | null
  signals: StockSignal[]
  constraints: ConstraintCheck[]
  alternatives: Alternative[]
  confidence: number
  needs_human_review: boolean
  used_fallback_policy: boolean
  idempotency_key: string
  rationale: string
  created_at: string
  decided_at: string | null
  decided_by: string | null
  simulator_response: unknown
  error_code: string | null
  error_message: string | null
  allocation_id: number | null
  sim_tick: number
}

export interface RecommendationReport {
  count: number
  auto_mode: boolean
  fallback_reason: string | null
  recommendations: Recommendation[]
}

export interface DecisionHistoryEntry {
  recommendation_id: string
  station_id: string
  fuel_type: FuelType
  source_depot_id: string
  route_id: string
  quantity: number
  status: RecommendationStatus
  risk_level: RiskLevel
  confidence: number
  decided_by: string | null
  decided_at: string | null
  allocation_id: number | null
  error_code: string | null
  rationale: string
  sim_tick: number
  created_at: string | null
}

export interface DecisionHistoryReport {
  count: number
  decisions: DecisionHistoryEntry[]
}

export interface DecisionSettings {
  auto_mode: boolean
  fallback_reason: string | null
  counters: Record<string, number>
  approval_rate: number
  conflicts_by_code: Record<string, number>
  policy: Record<string, unknown>
  fallback_policy: { approach: string; used: boolean }
}

/** What the what-if endpoint returns. */
export interface SimulationResult {
  recommendation_id: string
  writes_to_simulator: boolean
  before: Record<string, unknown>
  after: Record<string, unknown>
  improvement: Record<string, unknown>
  projection: RiskProjection | null
}

// -- simulation control ------------------------------------------------------

export interface SimulationControl {
  simulation_only: boolean
  status: 'PAUSED' | 'RUNNING' | 'UNKNOWN'
  tick: number | null
  sim_time: string | null
  tick_minutes: number | null
  scenario_id: string | null
  actions: string[]
}

export interface ServiceHealth {
  components: {
    backend: { status: string }
    database: { status: string; error?: string }
    simulator: { status: string; error?: string; simulation?: { status: string; tick: number } }
    prediction_service: {
      status: string
      active_model: string
      evaluated_at: string | null
      forecast_count: number
    }
    decision_engine: {
      status: string
      auto_mode: boolean
      fallback_active: boolean
      fallback_reason: string | null
      counters: Record<string, number>
      approval_rate: number
      decisions_per_minute: number
    }
  }
  http: {
    p95_latency_seconds: number
    request_count: number
    error_count: number
    error_rate: number
  }
}
