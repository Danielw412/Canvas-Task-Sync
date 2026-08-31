import { ArrowRight, CalendarDays, Filter, Play, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import { Button, EmptyState, StatusLabel } from '../components/ui'
import { useApp } from '../components/AppContext'
import { agendaWeekOptions, fetchJson, formatDateTime, formatDuration, mutateJson, useOverview } from '../lib/api'
import type { RunStatus, RunSummary, WeekSelection } from '../types'

export default function RunsPage() {
  const { selectedCourseId, toast } = useApp()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<RunStatus | ''>('')
  const [courseFilter, setCourseFilter] = useState('')
  const [weekSelection, setWeekSelection] = useState<WeekSelection>('this_week')
  const { data: overview } = useOverview(selectedCourseId)
  const course = overview?.courses.find((item) => item.id === overview.selected_course_id)
  const weekOptions = agendaWeekOptions(course?.settings.timezone)
  const suffix = new URLSearchParams({ ...(courseFilter ? { course_id: courseFilter } : {}), ...(status ? { status } : {}) })
  const runsUrl = suffix.size ? `/api/v1/runs?${suffix}` : '/api/v1/runs'
  const { data: runs, error } = useSWR<RunSummary[]>(runsUrl, fetchJson)
  const navigate = useNavigate()
  const filtered = useMemo(() => (runs ?? []).filter((run) => `${run.course_name} ${run.status} ${run.requested_mode}`.toLowerCase().includes(query.toLowerCase())), [runs, query])

  async function syncCourse() {
    if (!selectedCourseId) return
    try {
      const result = await mutateJson<{ run_id: number }>('/api/v1/runs', { body: { course_id: selectedCourseId, mode: 'auto_apply', week_selection: weekSelection } })
      navigate(`/runs/${result.run_id}`)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Run could not be started.', 'error')
    }
  }

  return <div className="standard-page">
    <header className="page-heading page-heading--actions"><div><h1>Runs</h1><p>Review previews, syncs, and health checks.</p></div><Button icon={Play} onClick={syncCourse} disabled={!selectedCourseId}>Sync selected course</Button></header>
    <div className="toolbar-row">
      <label className="filter-control week-filter-control"><CalendarDays size={17} /><select aria-label="Agenda week" value={weekSelection} onChange={(event) => setWeekSelection(event.target.value as WeekSelection)}>{weekOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>
      <label className="search-control"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search runs" /></label>
      <label className="filter-control"><Filter size={17} /><select aria-label="Course filter" value={courseFilter} onChange={(event) => setCourseFilter(event.target.value)}><option value="">All courses</option>{overview?.courses.map((item) => <option value={item.id} key={item.id}>{item.settings.name}</option>)}</select></label>
      <label className="filter-control"><Filter size={17} /><select value={status} onChange={(event) => setStatus(event.target.value as RunStatus | '')}><option value="">All statuses</option><option value="awaiting_approval">Ready to review</option><option value="succeeded">Success</option><option value="review_needed">Review needed</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select></label>
    </div>
    {error ? <EmptyState title="Runs could not load" body={error.message} /> : filtered.length ? <div className="table-frame">
      <div className="data-table data-table--runs-page data-table__header"><span>Started</span><span>Course</span><span>Mode</span><span>Result</span><span>Duration</span><span>Changes</span><span>Trigger</span><span /></div>
      {filtered.map((run) => <Link className="data-table data-table--runs-page data-row" to={`/runs/${run.id}`} key={run.id}><span>{formatDateTime(run.created_at)}</span><span>{run.course_name ?? run.course_id}</span><span>{run.requested_mode === 'auto_apply' ? 'Auto-apply' : run.requested_mode === 'health' ? 'Health check' : 'Dry run'}</span><StatusLabel status={run.status} /><span>{formatDuration(run.started_at, run.finished_at)}</span><span>{(run.counts.create ?? 0) + (run.counts.update ?? 0) + (run.counts.notes_cleanup ?? 0)} changes</span><span>{run.trigger === 'schedule' ? 'Schedule' : 'Manual'}</span><ArrowRight size={16} /></Link>)}
    </div> : <EmptyState title="No matching runs" body="Change the filter or start a new sync." action={<Button icon={Play} onClick={syncCourse}>Sync selected course</Button>} />}
  </div>
}
