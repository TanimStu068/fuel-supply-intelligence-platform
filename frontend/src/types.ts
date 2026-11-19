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
  fuel_type: 'DIESEL' | 'PETROL' | 'OCTANE'
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
  fuel_type: 'DIESEL' | 'PETROL' | 'OCTANE'
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

export interface StateSnapshot {
  instance: Instance
  regions: Region[]
  depots: Depot[]
  stations: Station[]
  routes: Route[]
  supply_arrivals: SupplyArrival[]
  events: SimulatorEvent[]
  allocations: Allocation[]
  metrics: Metrics
  stale: boolean
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
