import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CourseView, OperationLogEvent, OperationSummary, RunDetail, RunStatus, RunSummary } from '../types'

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

function runProgressLabel(run: RunSummary) {
  if (run.stage === 'complete' && ['succeeded', 'review_needed', 'stale'].includes(run.status)) {
    return 'Complete'
  }
  return `${runStatusLabel(run.status)} · ${humanizeStage(run.stage)}`
}

function runOutcome(run: RunSummary) {
  if (run.error_summary) return { message: run.error_summary, tone: 'error' }
  const outcomes: Partial<Record<RunStatus, { message: string; tone: string }>> = {
    awaiting_approval: { message: 'Approval required', tone: 'warning' },
    review_needed: { message: 'Review needed', tone: 'warning' },
    stale: { message: 'Preview changed — run again', tone: 'warning' },
    cancelled: { message: 'Run cancelled', tone: 'muted' },
    failed: { message: 'Run failed', tone: 'error' },
    failed_partial: { message: 'Run partially applied', tone: 'error' },
    succeeded: { message: 'No errors', tone: 'success' },
  }
  return outcomes[run.status] ?? { message: 'In progress', tone: 'muted' }
}

function dueDateLabel(dueDate?: string | null) {
  if (!dueDate) return 'No due date'
  return new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  }).format(new Date(`${dueDate}T12:00:00`))
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
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')

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

  const selectedRun = runs.find((run) => run.id === selectedRunId)
  const selectedRunStatus = selectedRun?.status
  const selectedRunStage = selectedRun?.stage

  useEffect(() => {
    if (!apiBase || selectedRunId === null) return
    let cancelled = false
    setDetailLoading(true)
    setDetailError('')
    void getJson<RunDetail>(apiBase, `/api/v1/runs/${selectedRunId}`)
      .then((detail) => {
        if (!cancelled) setRunDetail(detail)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setRunDetail(null)
          setDetailError(error instanceof Error ? error.message : 'Run details could not load.')
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false)
      })
    return () => { cancelled = true }
  }, [apiBase, selectedRunId, selectedRunStage, selectedRunStatus])

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

  function toggleRun(runId: number) {
    setRunDetail(null)
    setDetailError('')
    setSelectedRunId((current) => current === runId ? null : runId)
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
        {runs.map((run) => {
          const expanded = selectedRunId === run.id
          const actions = expanded ? runDetail?.plan?.actions ?? [] : []
          const outcome = runOutcome(run)
          return <div className="run-status-item" key={run.id}>
            <button className="run-status-row" type="button" aria-expanded={expanded} aria-controls={`run-plan-${run.id}`} onClick={() => toggleRun(run.id)}>
              <span className={`run-marker run-marker--${run.status}`} aria-hidden="true" />
              <strong>{run.course_name ?? run.course_id}</strong>
              <span>Run #{run.id}</span>
              <span>{runProgressLabel(run)}</span>
              <span className={`run-outcome run-outcome--${outcome.tone}`}>{outcome.message}</span>
              <span className="run-disclosure" aria-hidden="true">{expanded ? '▴' : '▾'}</span>
            </button>
            {expanded ? <section className="run-plan-details" id={`run-plan-${run.id}`} aria-label={`Suggested tasks for run ${run.id}`}>
              <div className="run-plan-heading"><strong>Suggested tasks</strong>{!detailLoading && !detailError ? <span>{actions.length} items</span> : null}</div>
              {detailLoading ? <p>Loading suggested tasks…</p> : detailError ? <p className="run-error">{detailError}</p> : actions.length ? <div className="run-plan-list">
                {actions.map((action, index) => <div className="run-plan-row" key={`${action.logical_id ?? action.title}-${index}`}>
                  <span className={`task-action task-action--${action.kind}`}>{humanizeStage(action.kind)}</span>
                  <strong>{action.title}</strong>
                  <span>{dueDateLabel(action.due_date)}</span>
                  <span>{action.task_list ?? '—'}</span>
                  <small>{action.reason}</small>
                </div>)}
              </div> : <p>{runDetail?.plan ? 'No tasks were suggested for this run.' : 'The suggested task plan is not available yet.'}</p>}
            </section> : null}
          </div>
        })}
      </div> : <p className="run-status-empty">No runs yet.</p>}
    </section>
  </main>
}
