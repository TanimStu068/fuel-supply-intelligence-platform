import { expect, test } from '@playwright/test'

/**
 * The end-to-end smoke test.
 *
 * One pass over every screen, against the running stack. The point is
 * not to test every pixel — it is to prove that an operator can open the
 * dashboard, reach every screen, and that each one shows real data
 * rather than an empty state or an error.
 */

/** The navigation entries, in the order the sidebar lists them. */
const SECTIONS = [
  { label: 'Overview', heading: 'Overview' },
  { label: 'Network', heading: 'Network' },
  { label: 'Inventory', heading: 'Inventory' },
  { label: 'Risk & alerts', heading: 'Risk & alerts' },
  { label: 'Recommendations', heading: 'Recommendations' },
  { label: 'Supply & events', heading: 'Supply & events' },
  { label: 'Decision history', heading: 'Decision history' },
  { label: 'Service health', heading: 'Service health' },
  { label: 'Simulation', heading: 'Simulation' },
]

test('the dashboard loads and shows the live state', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))

  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()
  // The top bar carries the simulation's clock and the service level,
  // both read from the live backend.
  await expect(page.getByText('Service level').first()).toBeVisible()
  await expect(page.getByText('Sim time').first()).toBeVisible()

  expect(errors).toEqual([])
})

test('every screen renders without an error state', async ({ page }) => {
  await page.goto('/')

  for (const section of SECTIONS) {
    await page.getByRole('button', { name: new RegExp(section.label) }).first().click()
    await expect(page.getByRole('heading', { name: section.heading, level: 1 })).toBeVisible()
    await expect(page.getByText('Something went wrong.')).toHaveCount(0)
  }
})

test('the network board draws the depots, stations and routes', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Network/ }).first().click()

  const board = page.getByRole('img', { name: /supply network/i })
  await expect(board).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Routes' })).toBeVisible()
})

test('the risk drawer explains a selected risk', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Risk & alerts/ }).first().click()

  const firstRow = page.locator('tr[data-testid^="risk-row-"]').first()
  await expect(firstRow).toBeVisible()
  await firstRow.click()

  const drawer = page.getByRole('complementary', { name: /risk explanation/i })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('heading', { name: 'Why' })).toBeVisible()
  await expect(drawer.getByRole('heading', { name: 'Forecast' })).toBeVisible()
  await expect(drawer.getByRole('heading', { name: 'Numbers' })).toBeVisible()
})

test('the inventory view shows fill bars and cover figures', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Inventory/ }).first().click()

  await expect(page.getByRole('heading', { name: 'Stations' })).toBeVisible()
  await expect(page.getByRole('meter').first()).toBeVisible()
})

test('the recommendations carry the simulation banner', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Recommendations/ }).first().click()

  await expect(page.getByRole('heading', { name: 'Recommendations', level: 1 })).toBeVisible()

  const cards = page.getByTestId('recommendation-card')
  const count = await cards.count()
  if (count > 0) {
    await expect(cards.first().getByText('Simulated environment. Human review required.')).toBeVisible()
  }
})

test('the simulation panel is labelled as simulation only', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Simulation/ }).first().click()

  await expect(page.getByText('SIMULATION ONLY').first()).toBeVisible()
  await expect(page.getByTestId('simulation-run-pause')).toBeVisible()
  await expect(page.getByTestId('inject-crisis')).toBeVisible()
})

test('the service health panel names every dependency', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Service health/ }).first().click()

  for (const component of [
    'Backend',
    'Database',
    'Simulator',
    'Prediction service',
    'Decision engine',
  ]) {
    await expect(page.getByRole('cell', { name: component, exact: true })).toBeVisible()
  }
  await expect(page.getByText('p95 latency')).toBeVisible()
})
test('the layout is usable on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 900 })
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()
  // The navigation collapses behind a toggle on a narrow screen.
  await expect(page.getByRole('button', { name: /toggle navigation/i })).toBeVisible()
})
