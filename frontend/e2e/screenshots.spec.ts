import { mkdir } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

/**
 * The screenshot capture.
 *
 * One picture per screen, written into `docs/screenshots/` so the
 * progress notes can show what the dashboard actually looks like.
 */

const SCREENSHOT_DIR = '../docs/screenshots'

/** Every screen, and the file name its picture is written to. */
const SCREENS = [
  { nav: /Overview/, file: 'overview.png', heading: 'Overview' },
  { nav: /Network/, file: 'network.png', heading: 'Network' },
  { nav: /Inventory/, file: 'inventory.png', heading: 'Inventory' },
  { nav: /Risk & alerts/, file: 'risk-alerts.png', heading: 'Risk & alerts' },
  { nav: /Recommendations/, file: 'recommendations.png', heading: 'Recommendations' },
  { nav: /Supply & events/, file: 'supply-events.png', heading: 'Supply & events' },
  { nav: /Decision history/, file: 'decision-history.png', heading: 'Decision history' },
  { nav: /Service health/, file: 'service-health.png', heading: 'Service health' },
  { nav: /Simulation/, file: 'simulation.png', heading: 'Simulation' },
]

test.beforeAll(async () => {
  await mkdir(SCREENSHOT_DIR, { recursive: true })
})

test('capture every screen', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()

  for (const screen of SCREENS) {
    await page.getByRole('button', { name: screen.nav }).first().click()
    await expect(page.getByRole('heading', { name: screen.heading, level: 1 })).toBeVisible()
    // Let the poll land before the picture, so the numbers are real.
    await page.waitForTimeout(1200)
    await page.screenshot({ path: `${SCREENSHOT_DIR}/${screen.file}`, fullPage: true })
  }
})

test('capture the risk explanation drawer', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Risk & alerts/ }).first().click()

  const firstRow = page.locator('tr[data-testid^="risk-row-"]').first()
  await expect(firstRow).toBeVisible()
  await firstRow.click()
  await expect(page.getByRole('complementary', { name: /risk explanation/i })).toBeVisible()
  await page.waitForTimeout(800)

  await page.screenshot({ path: `${SCREENSHOT_DIR}/risk-explanation-drawer.png`, fullPage: true })
})

test('capture the narrow layout', async ({ page }) => {
  await page.setViewportSize({ width: 430, height: 1000 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()
  await page.waitForTimeout(1200)

  await page.screenshot({ path: `${SCREENSHOT_DIR}/responsive-mobile.png`, fullPage: true })
})
