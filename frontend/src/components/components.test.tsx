/**
 * The component smoke tests.
 *
 * Each test covers one thing an operator depends on: that the numbers
 * reach the screen, that the warnings appear, and that the buttons do
 * what they say.
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ForecastChart } from '../components/ForecastChart'
import { HealthBanner } from '../components/AppShell'
import { InventoryView } from '../views/InventoryView'
import { NetworkView } from '../views/NetworkView'
import { OverviewView } from '../views/OverviewView'
import { RecommendationsView, SIMULATION_BANNER } from '../views/RecommendationsView'
import { RiskView } from '../views/RiskView'
import { SimulationView } from '../views/SimulationView'
import {
  alertsFixture,
  forecastFixture,
  recommendationFixture,
  risksFixture,
  serviceHealthFixture,
  simulationFixture,
  stateFixture,
} from '../test/fixtures'

const noop = () => {}

describe('OverviewView', () => {
  it('shows the service level, the unmet demand and the tick', () => {
    render(
      <OverviewView
        state={stateFixture}
        risks={risksFixture}
        alerts={alertsFixture}
        recommendations={[recommendationFixture]}
        serviceHealth={serviceHealthFixture}
      />,
    )

    expect(screen.getByText('97.77%')).toBeInTheDocument()
    expect(screen.getByText('4,200 L')).toBeInTheDocument()
    expect(screen.getByText('253')).toBeInTheDocument()
  })

  it('lists the stations needing fuel, worst first', () => {
    render(
      <OverviewView
        state={stateFixture}
        risks={risksFixture}
        alerts={alertsFixture}
        recommendations={[]}
        serviceHealth={serviceHealthFixture}
      />,
    )

    const rows = screen
      .getAllByRole('row')
      .filter((row) => row.textContent?.includes('station-'))
    expect(rows[0].textContent).toContain('station-mirpur')
    expect(rows[0].textContent).toContain('CRITICAL')
  })

  it('reports the platform health in the panel', () => {
    render(
      <OverviewView
        state={stateFixture}
        risks={risksFixture}
        alerts={alertsFixture}
        recommendations={[]}
        serviceHealth={serviceHealthFixture}
      />,
    )

    expect(screen.getByText('42.0 ms')).toBeInTheDocument()
    expect(screen.getAllByText('Prediction service').length).toBeGreaterThan(0)
  })
})

describe('NetworkView', () => {
  it('draws the two depots, the four stations and the six routes', () => {
    const { container } = render(
      <NetworkView
        depots={stateFixture.depots}
        stations={stateFixture.stations}
        routes={stateFixture.routes}
        regions={stateFixture.regions}
      />,
    )

    expect(container.querySelector('.view-subtitle')?.textContent).toContain(
      '2 depots, 4 stations, 6 routes across 2 regions',
    )
    expect(screen.getByRole('img', { name: /supply network/i })).toBeInTheDocument()
  })

  it('shows the live status of each route, depot and station', () => {
    render(
      <NetworkView
        depots={stateFixture.depots}
        stations={stateFixture.stations}
        routes={stateFixture.routes}
        regions={stateFixture.regions}
      />,
    )

    expect(screen.getAllByText('DISRUPTED').length).toBeGreaterThan(0)
    expect(screen.getAllByText('CONSTRAINED').length).toBeGreaterThan(0)
    expect(screen.getAllByText('OUTAGE').length).toBeGreaterThan(0)
  })
})

describe('InventoryView', () => {
  it('shows a fill bar and a cover figure for every station and fuel', () => {
    render(
      <InventoryView
        stations={stateFixture.stations}
        depots={stateFixture.depots}
        risks={risksFixture}
      />,
    )

    expect(screen.getByText('2,100 L')).toBeInTheDocument()
    expect(screen.getByText(/15,000 L/)).toBeInTheDocument()
    expect(screen.getByText('3.2 h')).toBeInTheDocument()
    expect(screen.getAllByRole('meter').length).toBeGreaterThan(0)
  })

  it('colours the fill bar by risk', () => {
    const { container } = render(
      <InventoryView
        stations={stateFixture.stations}
        depots={stateFixture.depots}
        risks={risksFixture}
      />,
    )

    expect(container.querySelector('.fill-bad')).toBeTruthy()
  })
})

describe('RiskView', () => {
  it('sorts the risks worst first', () => {
    render(<RiskView risks={risksFixture} alerts={alertsFixture} forecasts={[forecastFixture]} />)

    const rows = screen
      .getAllByRole('row')
      .filter((row) => row.textContent?.includes('station-'))
    expect(rows[0].textContent).toContain('station-mirpur')
    expect(rows[0].textContent).toContain('CRITICAL')
  })

  it('opens an explanation drawer with the signals and the forecast', async () => {
    const user = userEvent.setup()
    render(<RiskView risks={risksFixture} alerts={alertsFixture} forecasts={[forecastFixture]} />)

    await user.click(screen.getByTestId('risk-row-station-mirpur-DIESEL'))

    const drawer = screen.getByRole('complementary', { name: /risk explanation/i })
    expect(within(drawer).getByText(/why/i)).toBeInTheDocument()
    expect(within(drawer).getByText('days of cover')).toBeInTheDocument()
    expect(within(drawer).getByRole('img', { name: /forecast demand/i })).toBeInTheDocument()
    expect(within(drawer).getByText(/upper bound/i)).toBeInTheDocument()
  })
})

describe('RecommendationsView', () => {
  const renderView = (overrides: Partial<Parameters<typeof RecommendationsView>[0]> = {}) =>
    render(
      <RecommendationsView
        recommendations={[recommendationFixture]}
        autoMode={false}
        fallbackReason={null}
        onApprove={noop}
        onReject={noop}
        onSimulate={noop}
        onPropose={noop}
        simulations={{}}
        busyIds={new Set()}
        actionErrors={{}}
        {...overrides}
      />,
    )

  it('puts the simulation banner on every recommendation', () => {
    renderView()

    const banners = screen.getAllByText(SIMULATION_BANNER)
    expect(banners).toHaveLength(1)
    expect(SIMULATION_BANNER).toBe('Simulated environment. Human review required.')
  })

  it('shows the expected impact, before and after', () => {
    renderView()

    const card = screen.getByTestId('recommendation-card')
    expect(within(card).getByText('Before')).toBeInTheDocument()
    expect(within(card).getByText('After')).toBeInTheDocument()
    expect(within(card).getByText('7,000 L')).toBeInTheDocument()
  })

  it('calls approve when the approve button is pressed', async () => {
    const user = userEvent.setup()
    const onApprove = vi.fn()
    renderView({ onApprove })

    await user.click(screen.getByTestId('approve-button'))
    expect(onApprove).toHaveBeenCalledWith('rec-1')
  })

  it('offers the alternatives behind a toggle', async () => {
    const user = userEvent.setup()
    renderView()

    expect(screen.queryByText(/depot-gazipur is constrained/i)).toBeNull()
    await user.click(screen.getByRole('button', { name: /show 1 alternative/i }))
    expect(screen.getByText(/depot-gazipur is constrained/i)).toBeInTheDocument()
    expect(screen.getByText(/route-patiya-coxsbazar/)).toBeInTheDocument()
  })

  it('shows the what-if result without writing to the simulator', () => {
    renderView({
      simulations: {
        'rec-1': {
          recommendation_id: 'rec-1',
          writes_to_simulator: false,
          before: { hours_to_stockout: 3.2, stockout_probability: 0.87 },
          after: { hours_to_stockout: 26.5, stockout_probability: 0.03 },
          improvement: {},
          projection: null,
        },
      },
    })

    const result = screen.getByTestId('what-if-result')
    expect(within(result).getByText('26.5 h')).toBeInTheDocument()
    expect(within(result).getByText('no')).toBeInTheDocument()
  })
})

describe('SimulationView', () => {
  const renderView = (overrides: Partial<Parameters<typeof SimulationView>[0]> = {}) =>
    render(
      <SimulationView
        control={simulationFixture}
        onControl={noop}
        onCrisis={noop}
        busy={false}
        error={null}
        lastAction={null}
        {...overrides}
      />,
    )

  it('labels the panel as simulation only', () => {
    renderView()

    expect(screen.getAllByText('SIMULATION ONLY').length).toBeGreaterThan(0)
    expect(screen.getByText('simulation only')).toBeInTheDocument()
  })

  it('runs the simulation when the run button is pressed', async () => {
    const user = userEvent.setup()
    const onControl = vi.fn()
    renderView({ onControl })

    await user.click(screen.getByTestId('simulation-run-pause'))
    expect(onControl).toHaveBeenCalledWith('run')
  })

  it('injects the selected crisis', async () => {
    const user = userEvent.setup()
    const onCrisis = vi.fn()
    renderView({ onCrisis })

    await user.selectOptions(screen.getByTestId('crisis-type'), 'route_disruption')
    await user.click(screen.getByTestId('inject-crisis'))
    expect(onCrisis).toHaveBeenCalledWith('route_disruption', 8, expect.any(Object))
  })
})

describe('HealthBanner', () => {
  it('says DEGRADED and gives the age of the cached data', () => {
    render(
      <HealthBanner
        state={{ ...stateFixture, degraded: true, stale: true, age_seconds: 42 }}
        health={null}
        serviceHealth={null}
      />,
    )

    expect(screen.getByText('DEGRADED')).toBeInTheDocument()
    expect(screen.getByText(/42 s ago/)).toBeInTheDocument()
  })

  it('says nothing when everything is healthy', () => {
    const { container } = render(
      <HealthBanner state={stateFixture} health={null} serviceHealth={serviceHealthFixture} />,
    )

    expect(container.querySelector('.banner')).toBeNull()
  })

  it('warns when the fallback policy is active', () => {
    render(
      <HealthBanner
        state={stateFixture}
        health={null}
        serviceHealth={{
          ...serviceHealthFixture,
          components: {
            ...serviceHealthFixture.components,
            decision_engine: {
              ...serviceHealthFixture.components.decision_engine,
              fallback_active: true,
              fallback_reason: 'the model is unavailable',
            },
          },
        }}
      />,
    )

    expect(screen.getByText('FALLBACK')).toBeInTheDocument()
  })
})

describe('ForecastChart', () => {
  it('draws the line and the confidence band', () => {
    const { container } = render(
      <ForecastChart points={forecastFixture.points} title="Predicted demand" />,
    )

    expect(container.querySelector('.chart-line')).toBeTruthy()
    expect(container.querySelector('.chart-band')).toBeTruthy()
  })

  it('says so when there is nothing to draw', () => {
    render(<ForecastChart points={[]} title="Predicted demand" />)
    expect(screen.getByText(/no forecast points/i)).toBeInTheDocument()
  })
})
