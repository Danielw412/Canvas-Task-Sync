import { useEffect, useState } from 'react'
import { formatDuration } from '../lib/api'

const TICK_MS = 100

// Owns its own clock so a running stopwatch re-renders only this text, not the whole page.
export function ElapsedTime({ start, finish, active }: { start?: string | null; finish?: string | null; active: boolean }) {
  const running = active && Boolean(start) && !finish
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setTick((value) => value + 1), TICK_MS)
    return () => window.clearInterval(timer)
  }, [running])
  return <>{formatDuration(start, finish)}</>
}
