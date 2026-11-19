/**
 * Runtime API configuration.
 *
 * Vite inlines `import.meta.env.VITE_*` at **build** time, so a value baked
 * into the image cannot be changed by editing the environment at run time.
 * This module therefore resolves the backend URL at runtime, in priority
 * order:
 *
 *   1. `window.__API_BASE_URL__` — injected by `/config.js` in `public/`
 *   2. `import.meta.env.VITE_API_BASE_URL` — build-time default
 *   3. same-origin (useful when the API is reverse-proxied behind the app)
 */
declare global {
  interface Window {
    __API_BASE_URL__?: string
  }
}

function resolveBaseUrl(): string {
  const runtime = window.__API_BASE_URL__
  if (runtime && runtime.trim() !== '') {
    return runtime.trim()
  }
  const buildTime = import.meta.env.VITE_API_BASE_URL
  if (buildTime && buildTime.trim() !== '') {
    return buildTime.trim()
  }
  return window.location.origin
}

export const API_BASE_URL = resolveBaseUrl().replace(/\/+$/, '')
