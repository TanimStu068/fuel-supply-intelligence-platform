const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8001').replace(
  /\/$/,
  '',
)

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error(`${path} failed with HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export const fetchHealth = () => getJson<import('./types').HealthReport>('/health')
export const fetchState = () => getJson<import('./types').StateSnapshot>('/api/state')
