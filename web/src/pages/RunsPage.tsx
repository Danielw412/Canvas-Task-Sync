import { ArrowRight, Filter, Play, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import { Button, EmptyState, StatusLabel } from '../components/ui'
import { useApp } from '../components/AppContext'
import { fetchJson, formatDateTime, formatDuration, mutateJson } from '../lib/api'
import type { RunStatus, RunSummary } from '../types'

export default function RunsPage() {
  const { selectedCourseId, toast } = useApp()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<RunStatus | ''>('')
  const suffix = new URLSearchParams({ ...(selectedCourseId ? { course_id: selectedCourseId } : {}), ...(status ? { status } : {}) })
  const { data: runs, error } = useSWR<RunSummary[]>(`/api/v1/runs?${suffix}`, fetchJson)
  const navigate = useNavigate()
  const filtered = useMemo(() => (runs ?? []).filter((run) => `${run.course_name} ${run.status} ${run.requested_mode}`.toLowerCase().includes(query.toLowerCase())), [runs, query])

  async function preview() {
    if (!selectedCourseId) return
    try {
      const result = await mutateJson<{ run_id: number }>('/api/v1/runs', { body: { course_id: selectedCourseId } })
      navigate(`/runs/${result.run_id}`)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Run could not be started.', 'error')
    }
  }

  return <div className="standard-page">
    <header className="page-heading page-heading--actions"><div><h1>Runs</h1><p>Review previews, applied changes, scheduled activity, and health checks.</p></div><Button icon={Play} onClick={preview} disabled={!selectedCourseId}>Preview sync</Button></header>
    <div className="toolbar-row">
      <label className="search-control"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search runs" /></label>
      <label className="filter-control"><Filter size={17} /><select value={status} onChange={(event) => setStatus(event.target.value as RunStatus | '')}><option value="">All statuses</option><option value="awaiting_approval">Ready to review</option><option value="succeeded">Success</option><option value="review_needed">Review needed</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select></label>
    </div>
    {error ? <EmptyState title="Runs could not load" body={error.message} /> : filtered.length ? <div className="table-frame">
      <div className="data-table data-table--runs-page data-table__header"><span>Started</span><span>Course</span><span>Mode</span><span>Result</span><span>Duration</span><span>Changes</span><span>Trigger</span><span /></div>
      {filtered.map((run) => <Link className="data-table data-table--runs-page data-row" to={`/runs/${run.id}`} key={run.id}><span>{formatDateTime(run.created_at)}</span><span>{run.course_name ?? run.course_id}</span><span>{run.requested_mode === 'auto_apply' ? 'Auto-apply' : run.requested_mode === 'health' ? 'Health check' : 'Dry run'}</span><StatusLabel status={run.status} /><span>{formatDuration(run.started_at, run.finished_at)}</span><span>{(run.counts.create ?? 0) + (run.counts.update ?? 0)} changes</span><span>{run.trigger === 'schedule' ? 'Schedule' : 'Manual'}</span><ArrowRight size={16} /></Link>)}
    </div> : <EmptyState title="No matching runs" body="Change the filter or start a new preview." action={<Button icon={Play} onClick={preview}>Preview sync</Button>} />}
  </div>
}
