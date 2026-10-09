import { CaretRightIcon, ClockCounterClockwiseIcon, MagnifyingGlassIcon, XCircleIcon } from '@phosphor-icons/react'
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import useSWR from 'swr'
import { ElapsedTime } from '../components/ElapsedTime'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, PageHeader, RunStatusBadge, SkeletonRows } from '../components/ui'
import {
  changeSummary,
  fetchJson,
  formatDayHeading,
  formatTime,
  isActiveRun,
  runKindLabel,
  useOverview,
} from '../lib/api'
import type { RunStatus, RunSummary } from '../types'

export default function RunsPage() {
  const { selectedCourseId } = useApp()
  const [searchParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<RunStatus | ''>('')
  const [courseFilter, setCourseFilter] = useState(searchParams.get('course') ?? '')
  const { data: overview } = useOverview(selectedCourseId)
  const params = new URLSearchParams({ ...(courseFilter ? { course_id: courseFilter } : {}), ...(status ? { status } : {}) })
  const runsUrl = params.size ? `/api/v1/runs?${params}` : '/api/v1/runs'
  const { data: runs, error, isLoading } = useSWR<RunSummary[]>(runsUrl, fetchJson, {
    refreshInterval: (data) => data?.some((run) => isActiveRun(run.status)) ? 2_000 : 0,
  })

  const groups = useMemo(() => {
    const term = query.trim().toLowerCase()
    const filtered = (runs ?? []).filter((run) => !term || `${run.course_name ?? run.course_id} ${runKindLabel(run)} ${run.status} ${run.error_summary ?? ''}`.toLowerCase().includes(term))
    const byDay = new Map<string, RunSummary[]>()
    for (const run of filtered) {
      const heading = formatDayHeading(run.created_at)
      byDay.set(heading, [...(byDay.get(heading) ?? []), run])
    }
    return [...byDay.entries()]
  }, [runs, query])

  const filtering = Boolean(query || status || courseFilter)

  function clearFilters() {
    setQuery('')
    setStatus('')
    setCourseFilter('')
  }

  return <div className="page--runs">
    <PageHeader title="Runs" description="Every sync, preview, and health check, newest first." />
    <div className="toolbar">
      <label className="search">
        <MagnifyingGlassIcon size={16} aria-hidden />
        <input className="control" aria-label="Search runs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search runs" />
      </label>
      <select className="control" aria-label="Course filter" value={courseFilter} onChange={(event) => setCourseFilter(event.target.value)}>
        <option value="">All courses</option>
        {overview?.courses.map((item) => <option value={item.id} key={item.id}>{item.settings.name}</option>)}
      </select>
      <select className="control" aria-label="Status filter" value={status} onChange={(event) => setStatus(event.target.value as RunStatus | '')}>
        <option value="">Any status</option>
        <option value="awaiting_approval">Ready to review</option>
        <option value="succeeded">Completed</option>
        <option value="review_needed">Needs review</option>
        <option value="failed">Failed</option>
        <option value="cancelled">Cancelled</option>
      </select>
      {filtering ? <Button variant="ghost" size="sm" onClick={clearFilters}>Clear filters</Button> : null}
    </div>

    {error ? <div className="surface"><EmptyState icon={XCircleIcon} title="Runs could not load" body={error.message} /></div>
      : isLoading && !runs ? <SkeletonRows rows={5} label="Loading runs" />
        : groups.length ? groups.map(([heading, items]) => <section className="group" key={heading} aria-label={heading}>
          <h2 className="group-label">{heading}<span>{items.length}</span></h2>
          <div className="surface">
            {items.map((run) => <Link className="row row--interactive run-row" to={`/runs/${run.id}`} key={run.id}>
              <span className="run-row__time">{formatTime(run.created_at)}</span>
              <span className="run-row__course">
                <span className="row-title truncate">{run.course_name ?? run.course_id}</span>
                <span className="row-meta">{runKindLabel(run)}{run.trigger === 'schedule' ? ', scheduled' : ''}</span>
              </span>
              <RunStatusBadge status={run.status} />
              <span className="run-row__changes truncate">{run.error_summary && ['failed', 'failed_partial'].includes(run.status) ? run.error_summary : changeSummary(run)}</span>
              <span className="run-row__duration"><ElapsedTime start={run.started_at} finish={run.finished_at} active={isActiveRun(run.status)} /></span>
              <CaretRightIcon className="row-chevron" size={15} aria-hidden />
            </Link>)}
          </div>
        </section>)
          : <div className="surface">{filtering
            ? <EmptyState icon={MagnifyingGlassIcon} title="No runs match these filters" body="Try another course or status, or clear the filters." action={<Button variant="secondary" onClick={clearFilters}>Clear filters</Button>} />
            : <EmptyState icon={ClockCounterClockwiseIcon} title="No runs yet" body="Runs appear here after you sync a course or a schedule fires." action={<Link className="btn btn--primary" to="/">Go to Overview</Link>} />}</div>}
  </div>
}
