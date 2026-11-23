/**
 * The polling hook.
 *
 * The dashboard reads live state, so it polls. Polling rather than SSE is
 * a deliberate choice: the backend's read paths are already served from
 * the cache the background sync keeps fresh, so a poll is cheap, and a
 * poll cannot leave the dashboard showing a stale frame forever the way a
 * dropped stream can.
 *
 * The hook keeps three things apart, because the dashboard has to show all
 * three at once:
 *
 *   - the **data**, which survives a failed refresh (the operator keeps
 *     looking at the last known state, with its age);
 *   - the **error**, which is shown next to the data rather than replacing
 *     it, because stale data with a warning is more useful than an empty
 *     screen;
 *   - the **loading** flag, which is only true before the first load.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'

export interface Polled<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** True while a refresh is in flight and there is already data. */
  refreshing: boolean
  /** The time of the last successful load. */
  lastLoadedAt: Date | null
  refresh: () => void
}

export function usePolled<T>(
  loader: () => Promise<T>,
  intervalMs: number,
  enabled = true,
): Polled<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null)
  const [nonce, setNonce] = useState(0)

  // The loader changes on every render unless it is stable, so it is kept
  // in a ref and the effect depends on the interval instead.
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!enabled) return

    let cancelled = false

    const load = async () => {
      if (data === null) setLoading(true)
      else setRefreshing(true)

      try {
        const result = await loaderRef.current()
        if (cancelled || !mounted.current) return
        setData(result)
        setError(null)
        setLastLoadedAt(new Date())
      } catch (cause) {
        if (cancelled || !mounted.current) return
        const message =
          cause instanceof ApiError
            ? cause.detail || cause.message
            : cause instanceof Error
              ? cause.message
              : String(cause)
        setError(message)
      } finally {
        if (!cancelled && mounted.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    }

    void load()
    const timer = window.setInterval(() => void load(), intervalMs)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
    // `data` is read to decide between the first-load and refresh
    // spinners; depending on it would restart the timer on every load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, nonce])

  const refresh = useCallback(() => setNonce((value) => value + 1), [])

  return { data, error, loading, refreshing, lastLoadedAt, refresh }
}
