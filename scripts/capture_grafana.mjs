/**
 * Capture the Grafana dashboards as PNGs.
 *
 * The drill calls this with the output directory. Playwright is already a
 * dependency of the frontend, so nothing has to be installed.
 *
 * Usage: node scripts/capture_grafana.mjs <output-dir>
 */

import { mkdir } from 'node:fs/promises'
import { chromium } from '../frontend/node_modules/playwright/index.mjs'

const outputDir = process.argv[2] ?? 'docs/screenshots/resilience'
const grafanaUrl = process.env.GRAFANA_URL ?? 'http://localhost:33000'

// The uid and the slug of each provisioned dashboard. The uid is the
// dashboard's Grafana identifier; the slug is its title, lower-cased,
// which is how Grafana builds the URL.
//
// The URL is the plain `/d/<uid>/<slug>`. Grafana's kiosk mode renders an
// empty shell when the page is loaded directly, so the screenshots are
// taken from the normal dashboard view instead.
const DASHBOARDS = [
  ['fuel-application', 'Application'],
  ['fuel-system', 'System'],
  ['fuel-intelligence', 'Intelligence'],
  ['fuel-integration', 'Integration'],
]

await mkdir(outputDir, { recursive: true })

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
const page = await context.newPage()

// Grafana asks for credentials. The login is done *through the page*, so
// the session cookie lands in the page's own cookie jar. Signing in
// through the API context sets the cookie in a different jar, and the
// dashboard then renders as "Not found" even though the cookie exists.
const adminUser = process.env.GRAFANA_ADMIN_USER ?? 'admin'
const adminPassword = process.env.GRAFANA_ADMIN_PASSWORD ?? 'change-me-in-env'

await page.goto(grafanaUrl + '/login', { waitUntil: 'networkidle', timeout: 45000 })
const loginStatus = await page.evaluate(
  async ([user, password]) => {
    const response = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, password }),
    })
    return response.status
  },
  [adminUser, adminPassword]
)
console.log('  signed in to Grafana (status ' + loginStatus + ')')

for (const pair of DASHBOARDS) {
  const uid = pair[0]
  const label = pair[1]
  const slug = label.toLowerCase()
  const url = grafanaUrl + '/d/' + uid + '/' + slug
  try {
    await page.goto(url, { waitUntil: 'networkidle', timeout: 45000 })
    // Grafana's client-side router resolves the dashboard on the *second*
    // load: the first one is a shell that renders "Not found" before the
    // session is applied. A reload gets the real dashboard.
    await page.reload({ waitUntil: 'networkidle', timeout: 45000 })
    // Wait for the dashboard grid to render. Grafana's React app hydrates
    // after the network goes idle, so a fixed delay is a race.
    await page.waitForSelector('.react-grid-item', { timeout: 30000 }).catch(() => {})
    // Let the panels finish their first query and draw their series.
    await page.waitForTimeout(6000)
    const panels = await page.locator('.react-grid-item').count()
    const path = outputDir + '/grafana-' + uid + '.png'
    await page.screenshot({ path })
    console.log('  captured ' + label + ' (' + panels + ' panels) -> ' + path)
  } catch (error) {
    console.log('  could not capture ' + label + ': ' + error.message)
  }
}

await browser.close()
