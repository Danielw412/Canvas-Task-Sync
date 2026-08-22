import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CourseView, OperationLogEvent, OperationSummary, RunStatus, RunSummary } from '../types'

interface RuntimeConfig {
  api_base: string
}

interface StartResponse {
  operation_id: string
  run_id?: number
  run_ids?: number[]
}

let csrfToken: string | null = null
const CLOSED_EVENT_SOURCE_STATE = 2
const defaultEventSourceFactory = (url: string) => new EventSource(url)

async function jsonResponse<T>(response: Response): Promise<T> {
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.error?.message ?? response.statusText ?? 'Request failed')
  }
  return payload as T
}

async function getJson<T>(apiBase: string, path: string): Promise<T> {
  return jsonResponse<T>(await fetch(`${apiBase}${path}`, { headers: { Accept: 'application/json' } }))
}

async function postJson<T>(apiBase: string, path: string, body: unknown): Promise<T> {
  csrfToken ??= (await getJson<{ csrf_token: string }>(apiBase, '/api/v1/bootstrap')).csrf_token
  return jsonResponse<T>(await fetch(`${apiBase}${path}`, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify(body),
  }))
}

function errorLog(error: unknown): OperationLogEvent {
  return {
    id: Date.now(),
    run_id: 0,
    sequence: 0,
    operation_id: 'local',
    course_id: 'local',
    course_name: 'Local UI',
    created_at: new Date().toISOString(),
    stage: 'complete',
    event_type: 'request_failed',
    level: 'error',
    message: error instanceof Error ? error.message : 'Request failed',
    metadata: {},
  }
}

const TERMINAL_RUN_STATUSES = new Set<RunStatus>([
  'awaiting_approval', 'succeeded', 'review_needed', 'stale', 'cancelled', 'failed', 'failed_partial',
])

function runStatusLabel(status: RunStatus) {
  const labels: Record<RunStatus, string> = {
    queued: 'Queued',
    running: 'In progress',
    awaiting_approval: 'Ready to review',
    applying: 'Applying',
    succeeded: 'Success',
    review_needed: 'Review needed',
    stale: 'Preview stale',
    cancelled: 'Cancelled',
    failed: 'Failed',
    failed_partial: 'Partially applied',
  }
  return labels[status]
}

function humanizeStage(stage: string) {
  return stage.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function statusFromLog(entry: OperationLogEvent, current: RunStatus): RunStatus {
  if (entry.event_type === 'run_failed') return entry.metadata.partial ? 'failed_partial' : 'failed'
  if (entry.event_type === 'run_cancelled') return 'cancelled'
  if (entry.event_type === 'run_completed' && typeof entry.metadata.status === 'string') {
    return entry.metadata.status as RunStatus
  }
  if (entry.stage === 'apply_changes' || entry.stage === 'revalidate_preview') return 'applying'
  return TERMINAL_RUN_STATUSES.has(current) ? current : 'running'
}

export function SimpleApp({
  eventSourceFactory = defaultEventSourceFactory,
}: {
  eventSourceFactory?: (url: string) => EventSource
}) {
  const [apiBase, setApiBase] = useState('')
  const [courses, setCourses] = useState<CourseView[]>([])
  const [operations, setOperations] = useState<OperationSummary[]>([])
  const [selectedCourse, setSelectedCourse] = useState('')
  const [selectedHistory, setSelectedHistory] = useState('')
  const [activeOperation, setActiveOperation] = useState('')
  const [liveOperation, setLiveOperation] = useState('')
  const [logs, setLogs] = useState<OperationLogEvent[]>([])
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [busy, setBusy] = useState(false)

  const refreshOperations = useCallback(async (base: string) => {
    setOperations(await getJson<OperationSummary[]>(base, '/api/v1/operations?limit=50'))
  }, [])

  const refreshRuns = useCallback(async (base: string) => {
    setRuns(await getJson<RunSummary[]>(base, '/api/v1/runs?limit=50'))
  }, [])

  useEffect(() => {
    let cancelled = false
    async function bootstrap() {
      try {
        const config = await jsonResponse<RuntimeConfig>(await fetch('/runtime-config.json'))
        const [courseItems, operationItems, runItems] = await Promise.all([
          getJson<CourseView[]>(config.api_base, '/api/v1/courses'),
          getJson<OperationSummary[]>(config.api_base, '/api/v1/operations?limit=50'),
          getJson<RunSummary[]>(config.api_base, '/api/v1/runs?limit=50'),
        ])
        if (cancelled) return
        setApiBase(config.api_base)
        setCourses(courseItems)
        setOperations(operationItems)
        setRuns(runItems)
        setSelectedCourse(courseItems.find((course) => course.settings.enabled)?.id ?? '')
      } catch (error) {
        if (!cancelled) setLogs([errorLog(error)])
      }
    }
    void bootstrap()
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!apiBase || !activeOperation) return
    const source = eventSourceFactory(
      `${apiBase}/api/v1/operations/${encodeURIComponent(activeOperation)}/events`,
    )
    const onLog = (event: MessageEvent<string>) => {
      const entry = JSON.parse(event.data) as OperationLogEvent
      setLogs((current) => [...current, entry])
      setRuns((current) => current.map((run) => run.id === entry.run_id ? {
        ...run,
        stage: entry.stage,
        status: statusFromLog(entry, run.status),
        error_summary: entry.level === 'error' ? entry.message : run.error_summary,
      } : run))
    }
    const onComplete = () => {
      source.close()
      if (activeOperation === liveOperation) setBusy(false)
      void Promise.all([refreshOperations(apiBase), refreshRuns(apiBase)])
    }
    source.addEventListener('log', onLog as EventListener)
    source.addEventListener('complete', onComplete)
    source.onerror = () => {
      if (source.readyState === CLOSED_EVENT_SOURCE_STATE && activeOperation === liveOperation) {
        setBusy(false)
      }
    }
    return () => source.close()
  }, [activeOperation, apiBase, eventSourceFactory, liveOperation, refreshOperations, refreshRuns])

  useEffect(() => {
    if (!apiBase || !busy) return
    const timer = window.setInterval(() => { void refreshRuns(apiBase) }, 1_000)
    return () => window.clearInterval(timer)
  }, [apiBase, busy, refreshRuns])

  const enabledCourses = useMemo(
    () => courses.filter((course) => course.settings.enabled),
    [courses],
  )
  const currentMessage = logs.at(-1)?.message ?? (busy ? 'Starting sync…' : 'Idle')

  async function start(path: '/api/v1/runs' | '/api/v1/runs/all', body: object) {
    if (!apiBase) return
    setLogs([])
    setSelectedHistory('')
    setActiveOperation('')
    setLiveOperation('')
    setBusy(true)
    try {
      const result = await postJson<StartResponse>(apiBase, path, { mode: 'auto_apply', ...body })
      await refreshRuns(apiBase)
      setLiveOperation(result.operation_id)
      setActiveOperation(result.operation_id)
    } catch (error) {
      setLogs([errorLog(error)])
      setBusy(false)
    }
  }

  function viewHistory(operationId: string) {
    setSelectedHistory(operationId)
    setLogs([])
    setBusy(false)
    setLiveOperation('')
    setActiveOperation(operationId)
  }

  return <main className="simple-shell">
    <header>
      <h1>Canvas Task Sync</h1>
      <span className={busy ? 'status status--busy' : 'status'}>{currentMessage}</span>
    </header>

    <section className="controls" aria-label="Sync controls">
      <button className="primary" disabled={busy || !enabledCourses.length} onClick={() => void start('/api/v1/runs/all', {})}>Sync all courses</button>
      <div className="course-control">
        <select aria-label="Course" value={selectedCourse} disabled={busy} onChange={(event) => setSelectedCourse(event.target.value)}>
          {enabledCourses.map((course) => <option key={course.id} value={course.id}>{course.settings.name}</option>)}
        </select>
        <button disabled={busy || !selectedCourse} onClick={() => void start('/api/v1/runs', { course_id: selectedCourse })}>Sync selected course</button>
      </div>
      <select aria-label="Historical logs" value={selectedHistory} disabled={busy} onChange={(event) => viewHistory(event.target.value)}>
        <option value="">View historical logs</option>
        {operations.map((operation) => <option value={operation.id} key={operation.id}>{new Date(operation.created_at).toLocaleString()} · {operation.course_names.join(', ')} · {operation.status}</option>)}
      </select>
    </section>

    <section className="console-section">
      <div className="console-heading"><strong>JSON log</strong><span>{logs.length} entries</span></div>
      <pre aria-label="JSON log output">{logs.length ? logs.map((entry) => JSON.stringify(entry)).join('\n') : '{"level":"info","message":"No sync log selected."}'}</pre>
    </section>

    <section className="run-status-section" aria-labelledby="run-status-heading">
      <div className="run-status-heading"><strong id="run-status-heading">Course run status</strong><span>{runs.length} recent runs</span></div>
      {runs.length ? <div className="run-status-list">
        {runs.map((run) => <div className="run-status-row" key={run.id}>
          <span className={`run-marker run-marker--${run.status}`} aria-label={runStatusLabel(run.status)} />
          <strong>{run.course_name ?? run.course_id}</strong>
          <span>Run #{run.id}</span>
          <span>{runStatusLabel(run.status)} · {humanizeStage(run.stage)}</span>
          <span className={run.error_summary ? 'run-error' : 'run-no-error'}>{run.error_summary ?? 'No errors'}</span>
        </div>)}
      </div> : <p className="run-status-empty">No runs yet.</p>}
    </section>
  </main>
}
