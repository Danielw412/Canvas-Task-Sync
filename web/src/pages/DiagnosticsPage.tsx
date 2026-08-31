import { AlertTriangle, ArrowRight, CheckCircle2, Download, HeartPulse, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, StatusIcon } from '../components/ui'
import { fetchJson, formatDateTime, mutateJson } from '../lib/api'
import type { DiagnosticsResponse, RunEvent } from '../types'

export default function DiagnosticsPage() {
  const { selectedCourseId, toast } = useApp()
  const { data, error, mutate } = useSWR<DiagnosticsResponse>('/api/v1/diagnostics', fetchJson)
  const [tab, setTab] = useState<'events' | 'evidence' | 'state' | 'errors'>('events')
  const navigate = useNavigate()
  async function runChecks() {
    try {
      const result = await mutateJson<{ run_id: number }>(`/api/v1/health-runs${selectedCourseId ? `?course_id=${encodeURIComponent(selectedCourseId)}` : ''}`)
      navigate(`/runs/${result.run_id}`)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Checks could not start.', 'error')
    }
  }
  if (error) return <EmptyState title="Diagnostics could not load" body={error.message} />
  const eventSets: Record<typeof tab, RunEvent[]> = {
    events: data?.recent_events ?? [],
    evidence: data?.recent_events.filter((event) => ['capture_source', 'extract_assignments', 'build_review_plan'].includes(event.stage)) ?? [],
    state: data?.recent_events.filter((event) => ['compare_google_tasks', 'revalidate_preview', 'persist_state'].includes(event.stage)) ?? [],
    errors: data?.recent_events.filter((event) => event.level !== 'info') ?? [],
  }
  return <div className="standard-page diagnostics-page">
    <header className="page-heading page-heading--actions"><div><h1>Diagnostics</h1><p>Inspect system health and sanitized run events.</p></div><div><a className="button button--secondary" href="/api/v1/diagnostics/support-bundle" download><Download size={17} />Support bundle</a><Button icon={HeartPulse} onClick={runChecks}>Run all checks</Button></div></header>
    <div className="diagnostics-grid">
      <section className="panel diagnostics-health"><div className="section-heading"><h2>System health</h2></div><div className="health-check-grid">{data?.checks.map((check) => <div className="diagnostic-check" key={check.key}><StatusIcon state={check.state} size={21} /><div><strong>{check.label}</strong><span>{check.summary}</span></div><ArrowRight size={15} /></div>)}</div></section>
      <section className="panel security-summary"><ShieldCheck size={24} /><div><h2>Sanitized by default</h2><p>Secrets, source images, and provider payloads stay out of diagnostics.</p></div></section>
      <section className="diagnostic-events panel"><div className="section-heading"><h2>Event stream</h2><button className="inline-link" onClick={() => void mutate()}>Refresh</button></div><div className="debug-tabs">{([['events', 'Events'], ['evidence', 'Evidence'], ['state', 'State decisions'], ['errors', 'Errors']] as const).map(([key, label]) => <button className={tab === key ? 'is-active' : ''} key={key} onClick={() => setTab(key)}>{label}</button>)}</div>{eventSets[tab].length ? <div className="diagnostic-event-list">{eventSets[tab].map((event) => <Link to={`/runs/${event.run_id}`} className="diagnostic-event" key={event.id}><time>{formatDateTime(event.created_at, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span className={`level level--${event.level}`}>{event.level === 'warning' ? 'WARN' : event.level.toUpperCase()}</span><div><strong>{event.message}</strong><small>{event.stage.replaceAll('_', ' ')} / Run #{event.run_id}</small></div>{event.duration_ms != null ? <span>{(event.duration_ms / 1000).toFixed(1)}s</span> : null}<ArrowRight size={15} /></Link>)}</div> : <EmptyState title={`No ${tab === 'state' ? 'state decisions' : tab}`} body="Run a sync or health check to populate this view." />}</section>
      <aside className="diagnostics-aside"><section className="rail-section"><h2>Recent errors</h2>{data?.error_runs.length ? data.error_runs.map((run) => <Link className="error-run" to={`/runs/${run.id}`} key={run.id}><AlertTriangle size={17} /><div><strong>{run.course_name ?? run.course_id}</strong><span>{run.error_summary ?? run.status}</span></div><ArrowRight size={15} /></Link>) : <div className="all-clear"><CheckCircle2 size={23} /><strong>No recent errors</strong><span>Failed and stale previews appear here.</span></div>}</section><section className="rail-section"><h2>Local storage</h2><dl className="path-list"><dt>Operational history</dt><dd>{data?.control_database ?? '-'}</dd><dt>Sync identity and cache</dt><dd>{data?.state_database ?? '-'}</dd></dl></section></aside>
    </div>
  </div>
}
