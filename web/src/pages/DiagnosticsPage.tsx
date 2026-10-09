import { ArrowClockwiseIcon, CaretRightIcon, CheckCircleIcon, DownloadSimpleIcon, PulseIcon, ShieldCheckIcon, XCircleIcon } from '@phosphor-icons/react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, PageHeader, Segmented, SkeletonRows, StatusIcon } from '../components/ui'
import { fetchJson, formatDateTime, formatRelative, formatTime, mutateJson, stageLabels, useOverview } from '../lib/api'
import type { DiagnosticsResponse, RunEvent } from '../types'

type EventFilter = 'events' | 'evidence' | 'state' | 'errors'

export default function DiagnosticsPage() {
  const { selectedCourseId, toast } = useApp()
  const { data, error, mutate, isValidating } = useSWR<DiagnosticsResponse>('/api/v1/diagnostics', fetchJson)
  const { data: overview } = useOverview(selectedCourseId)
  const [filter, setFilter] = useState<EventFilter>('events')
  const [checkCourse, setCheckCourse] = useState('')
  const [starting, setStarting] = useState(false)
  const navigate = useNavigate()
  const course = checkCourse || selectedCourseId || ''

  async function runChecks() {
    setStarting(true)
    try {
      const result = await mutateJson<{ run_id: number }>(`/api/v1/health-runs${course ? `?course_id=${encodeURIComponent(course)}` : ''}`)
      navigate(`/runs/${result.run_id}`)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Checks could not start.', 'error')
      setStarting(false)
    }
  }

  if (error) return <div className="surface"><EmptyState icon={XCircleIcon} title="Diagnostics could not load" body={error.message} /></div>

  const events = data?.recent_events ?? []
  const eventSets: Record<EventFilter, RunEvent[]> = {
    events,
    evidence: events.filter((event) => ['capture_source', 'extract_assignments', 'build_review_plan'].includes(event.stage)),
    state: events.filter((event) => ['compare_google_tasks', 'revalidate_preview', 'persist_state'].includes(event.stage)),
    errors: events.filter((event) => event.level !== 'info'),
  }

  return <div className="page--diagnostics">
    <PageHeader
      title="Diagnostics"
      description="Connection checks, recent problems, and the sanitized event log."
      actions={<>
        <a className="btn btn--secondary" href="/api/v1/diagnostics/support-bundle" download><DownloadSimpleIcon size={16} aria-hidden />Support bundle</a>
        {overview?.courses.length ? <select className="control" aria-label="Course to check" value={course} onChange={(event) => setCheckCourse(event.target.value)}>
          {overview.courses.map((item) => <option key={item.id} value={item.id}>{item.settings.name}</option>)}
        </select> : null}
        <Button icon={PulseIcon} loading={starting} onClick={() => void runChecks()}>Run health check</Button>
      </>}
    />

    <section className="section" aria-labelledby="checks-heading">
      <div className="section-head"><h2 id="checks-heading">Connections</h2><p>From the server's last look. Run a health check for a live test.</p></div>
      {!data ? <SkeletonRows rows={3} label="Loading checks" /> : data.checks.length ? <div className="surface">
        {data.checks.map((check) => <div className="row check-row" key={check.key}>
          <StatusIcon state={check.state} />
          <span className="row-title">{check.label}</span>
          <span className="check-row__summary">{check.summary}</span>
          <span className="subtle">{check.checked_at ? formatRelative(check.checked_at) : ''}</span>
        </div>)}
      </div> : <div className="surface"><EmptyState title="No checks reported" body="Run a health check to test each connection." /></div>}
    </section>

    {data?.error_runs.length ? <section className="section" aria-labelledby="problems-heading">
      <div className="section-head"><h2 id="problems-heading">Recent problems</h2><p>Failed runs and previews that went out of date.</p></div>
      <div className="surface">
        {data.error_runs.map((run) => <Link className="row row--interactive problem-row" to={`/runs/${run.id}`} key={run.id}>
          <StatusIcon state={run.status} />
          <span className="row-title truncate">{run.course_name ?? run.course_id}</span>
          <span className="row-meta truncate">{run.error_summary ?? (run.status === 'stale' ? 'Preview went out of date' : 'Run failed')}</span>
          <span className="subtle num">{formatDateTime(run.created_at)}</span>
          <CaretRightIcon className="row-chevron" size={15} aria-hidden />
        </Link>)}
      </div>
    </section> : data ? <p className="footnote"><CheckCircleIcon className="tone-success" size={16} weight="fill" aria-hidden />No failed runs recently.</p> : null}

    <section className="section" aria-labelledby="events-heading">
      <div className="section-head">
        <h2 id="events-heading">Event log</h2>
        <div className="section-head__aside">
          <Segmented
            mode="buttons"
            label="Event filter"
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'events', label: 'All' },
              { value: 'evidence', label: 'Evidence' },
              { value: 'state', label: 'State' },
              { value: 'errors', label: 'Problems', count: eventSets.errors.length },
            ]}
          />
          <Button variant="ghost" size="sm" icon={ArrowClockwiseIcon} loading={isValidating} onClick={() => void mutate()}>Refresh</Button>
        </div>
      </div>
      {!data ? <SkeletonRows rows={4} label="Loading events" /> : eventSets[filter].length ? <div className="surface">
        {eventSets[filter].map((event) => <Link to={`/runs/${event.run_id}`} className="row row--interactive event-line" key={event.id}>
          <time className="num">{formatTime(event.created_at)}</time>
          <span className={`level level--${event.level}`}>{event.level === 'warning' ? 'WARN' : event.level.toUpperCase()}</span>
          <span className="event-line__text"><span className="truncate">{event.message}</span><small>{stageLabels[event.stage] ?? event.stage}, run #{event.run_id}</small></span>
          <span className="event-line__duration">{event.duration_ms != null ? `${(event.duration_ms / 1000).toFixed(1)}s` : ''}</span>
          <CaretRightIcon className="row-chevron" size={15} aria-hidden />
        </Link>)}
      </div> : <div className="surface"><EmptyState title="Nothing logged here" body="Sync a course or run a health check to fill the log." /></div>}
      <p className="fine-print"><ShieldCheckIcon size={14} aria-hidden />Secrets, source images, and provider payloads are never written to this log.</p>
    </section>

    <section className="section" aria-labelledby="storage-heading">
      <div className="section-head"><h2 id="storage-heading">Local storage</h2></div>
      <div className="paths">
        <span>Run history and settings: <code>{data?.control_database ?? '-'}</code></span>
        <span>Sync identity and cache: <code>{data?.state_database ?? '-'}</code></span>
      </div>
    </section>
  </div>
}
