import { useCallback, useEffect, useRef, useState } from 'react'

/** Minimal async-resource hook — no SWR, no store (spec §01). */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)

  const run = useCallback(() => {
    let alive = true
    setLoading(true)
    setError(null)
    fn()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(run, [run])
  return { data, error, loading, reload: run }
}

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const on = () => setReduced(mq.matches)
    on()
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return reduced
}

/** Tab-visibility flag — used to pause polling in a backgrounded tab (§07). */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  )
  useEffect(() => {
    const on = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', on)
    return () => document.removeEventListener('visibilitychange', on)
  }, [])
  return visible
}

/** Spring count-up to a target, once per key change (§06.1). */
export function useCountUp(target: number, key: unknown, disabled: boolean) {
  const [value, setValue] = useState(disabled ? target : 0)
  const seen = useRef<unknown>(undefined)

  useEffect(() => {
    if (disabled) {
      setValue(target)
      return
    }
    if (seen.current === key) return
    seen.current = key
    const duration = 700
    const start = performance.now()
    let raf = 0
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      // spring-ish ease-out
      const eased = 1 - Math.pow(1 - t, 3)
      setValue(Math.round(target * eased))
      if (t < 1) raf = requestAnimationFrame(tick)
    }
    setValue(0)
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target, key, disabled])

  return value
}
