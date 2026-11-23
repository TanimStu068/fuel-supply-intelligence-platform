/**
 * Rasterize `docs/assets/architecture.svg` into `docs/assets/architecture.png`.
 *
 * The architecture diagram is authored as SVG so it stays editable, but the
 * README embeds a raster image because GitHub renders SVG attachments as
 * untrusted and refuses to display them.
 *
 * Usage:
 *   node scripts/render_architecture.mjs
 *
 * Requires Playwright's Chromium, which the frontend already installs for
 * the end-to-end tests: `cd frontend && npm install && npx playwright install chromium`.
 */

import { createRequire } from 'node:module'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDirectory = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(scriptDirectory, '..')

// playwright-core lives in the frontend's node_modules, so it is resolved
// from there rather than from this script's own directory.
const require = createRequire(resolve(repoRoot, 'frontend', 'package.json'))
const { chromium } = require('playwright-core')

const sourcePath = resolve(repoRoot, 'docs', 'assets', 'architecture.svg')
const targetPath = resolve(repoRoot, 'docs', 'assets', 'architecture.png')

const svgMarkup = await readFile(sourcePath, 'utf8')

// The SVG is inlined into a minimal HTML page rather than loaded from a
// file:// URL: a direct SVG navigation never settles on a network-idle
// signal, so the screenshot would race the paint.
const pageMarkup = `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      html, body { margin: 0; padding: 0; background: #ffffff; }
      svg { display: block; }
    </style>
  </head>
  <body>${svgMarkup}</body>
</html>`

const browser = await chromium.launch({ args: ['--no-sandbox'] })
try {
  const page = await browser.newPage({ deviceScaleFactor: 2 })
  await page.setContent(pageMarkup, { waitUntil: 'load' })
  const diagram = page.locator('svg').first()
  await diagram.waitFor({ state: 'visible' })
  // The white background is drawn inside the SVG itself, so the picture is
  // opaque white everywhere.
  await diagram.screenshot({ path: targetPath })
  console.log(`wrote ${targetPath}`)
} finally {
  await browser.close()
}
