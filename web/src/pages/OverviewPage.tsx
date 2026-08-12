import {
  ArrowRight,
  CalendarDays,
  Check,
  CheckCircle2,
  Clock3,
  FileSliders,
  Play,
  Presentation,
  Sparkles,
  Target,
} from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { mutate } from 'swr'
import { formatDateTime, formatDuration, mutateJson, useOverview } from '../lib/api'
import type { ExtractionMode, RunSummary } from '../types'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, Modal, StatusIcon, StatusLabel } from '../components/ui'

const planKinds = [
  { key: 'create', label: 'Create', detail: 'New tasks to create', tone: 'success' },
  { key: 'update', label: 'Update', detail: 'Tasks to update', tone: 'primary' },
  { key: 'unchanged', label: 'Unchanged', detail: 'No changes needed', tone: 'muted' },
  { key: 'attention', label: 'Needs attention', detail: 'Conflicts or issues', tone: 'warning' },
] as const

const flowStages = [
  { label: 'Source', icon: Presentation },
  { label: 'Extraction', icon: Sparkles },
  { label: 'Deadline policy', icon: CalendarDays },
  { label: 'Reconciliation', icon: FileSliders },
]

function attentionCount(run?: RunSummary | null) {
  if (!run) return 0
  return ['uncertain', 'source_missing', 'remote_missing', 'historical_blocked']
    .reduce((sum, key) => sum + (run.counts[key] ?? 0), 0)
}

export default function OverviewPage() {
  const { selectedCourseId, toast } = useApp()
  const { data, error, isLoading } = useOverview(selectedCourseId)
  const navigate = useNavigate()
  const course = data?.courses.find((item) => item.id === data.selected_course_id)
  const [mode, setMode] = useState<ExtractionMode | ''>('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [includePast, setIncludePast] = useState(false)
  const [rebaseWeek, setRebaseWeek] = useState('')
  const selectedMode = mode || course?.settings.source.extraction.mode || 'hybrid'

  async function previewSync(advanced = false) {
    if (!course) return
    if (advanced && rebaseWeek && new Date(`${rebaseWeek}T12:00:00`).getDay() !== 1) {
      toast('The test week must begin on a Monday.', 'warning')
      return
    }
    try {
      const result = await mutateJson<{ run_id: number }>('/api/v1/runs', {
        body: {
          course_id: course.id,
          extraction_mode: selectedMode,
          include_past: advanced ? includePast : false,
          test_rebase_week: advanced && rebaseWeek ? rebaseWeek : undefined,
        },
      })
      setAdvancedOpen(false)
      await mutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
      navigate(`/runs/${result.run_id}`)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Preview could not be started.', 'error')
    }
  }

  if (error) return <EmptyState title="Overview could not load" body={error.message} />
  const connected = Boolean(data?.connections.google_authorized && data.connections.gemini_configured)
  const latest = data?.latest_run
  const values: Record<string, number> = {
    create: latest?.counts.create ?? 0,
    update: latest?.counts.update ?? 0,
    unchanged: latest?.counts.unchanged ?? 0,
    attention: attentionCount(latest),
  }

  return <div className="overview-layout">
    <section className="overview-main">
      <header className="page-heading overview-heading">
        <div><h1>{connected ? 'Everything is ready to sync' : 'Finish setup to start syncing'}</h1><p>Review the source, preview changes, and apply only what you approve.</p></div>
        <div className="overview-actions">
          <Button icon={Play} onClick={() => void previewSync()} disabled={!course?.settings.enabled || isLoading}>Preview sync</Button>
          <label className="select-control"><span className="sr-only">Extraction mode</span><select value={selectedMode} onChange={(event) => setMode(event.target.value as ExtractionMode)}><option value="hybrid">Hybrid extraction</option><option value="auto">Auto extraction</option><option value="image">Image extraction</option><option value="text">Text extraction</option></select></label>
          <Button variant="ghost" className="advanced-preview-button" onClick={() => setAdvancedOpen(true)}>Advanced preview</Button>
        </div>
      </header>

      <section className="source-workspace panel">
        <div className="source-summary">
          <div className="source-summary__item"><span className="source-icon source-icon--slides"><Presentation size={19} /></span><div><span>Source</span><strong>Google Slides · Agenda page</strong></div></div>
          <div className="source-summary__item"><span className="source-icon source-icon--tasks"><Target size={19} /></span><div><span>Task destination</span><strong>Google Tasks · {course?.settings.task_list ?? 'Not configured'}</strong></div></div>
          <div className="source-summary__item"><Clock3 size={20} /><div><span>Last checked</span><strong>{latest ? formatDateTime(latest.finished_at ?? latest.created_at) : 'Not checked yet'}</strong></div></div>
        </div>
        <div className="mobile-health-summary">
          <div><span className="source-icon source-icon--slides"><Presentation size={19} /></span><strong>Google Slides</strong><small className={connected ? 'tone-success' : 'tone-warning'}>{connected ? 'Accessible' : 'Check setup'}</small></div>
          <div><span className="source-icon source-icon--gemini"><Sparkles size={19} /></span><strong>Gemini</strong><small className={data?.connections.gemini_configured ? 'tone-success' : 'tone-warning'}>{data?.connections.gemini_configured ? 'Connected' : 'Missing'}</small></div>
          <div><span className="source-icon source-icon--tasks"><Target size={19} /></span><strong>Google Tasks</strong><small className={data?.connections.google_authorized ? 'tone-success' : 'tone-warning'}>{data?.connections.google_authorized ? 'Accessible' : 'Check setup'}</small></div>
          <p>{connected ? 'All systems healthy' : 'Setup needs attention'}</p>
        </div>
        <div className="flow-steps" aria-label="Sync stages">
          {flowStages.map(({ label, icon: Icon }, index) => <div className="flow-step" key={label}><div className="flow-step__line">{index ? <span /> : null}<i className={connected ? 'is-healthy' : ''}><Icon size={17} /></i>{index < 3 ? <span /> : null}</div><strong>{label}</strong><small className={connected ? 'tone-success' : 'tone-muted'}>{connected ? 'Healthy' : 'Waiting'}</small></div>)}
        </div>
      </section>

      <section className="plan-band panel" aria-label="Latest plan summary">
        {planKinds.map((item) => <div className="plan-metric" key={item.key}><span className={`plan-metric__value tone-${item.tone}`}>{values[item.key]}</span><div><strong>{item.label}</strong><small>{item.detail}</small></div></div>)}
        {latest ? <Link className="inline-link plan-band__link" to={`/runs/${latest.id}`}>Review latest plan <ArrowRight size={17} /></Link> : <span className="plan-band__empty">No preview yet</span>}
      </section>

      <section className="recent-section">
        <div className="section-heading"><h2>Recent runs</h2><Link className="inline-link" to="/runs">View all runs <ArrowRight size={17} /></Link></div>
        {data?.recent_runs.length ? <div className="table-frame recent-runs-table">
          <div className="data-table data-table--runs data-table__header"><span>Started</span><span>Course</span><span>Mode</span><span>Result</span><span>Duration</span><span>Changes</span><span>Trigger</span><span /></div>
          {data.recent_runs.map((run) => <Link to={`/runs/${run.id}`} className="data-table data-table--runs data-row" key={run.id}>
            <span>{formatDateTime(run.started_at ?? run.created_at)}</span><span>{run.course_name ?? run.course_id}</span><span className="run-mode">{run.requested_mode === 'health' ? 'Health check' : run.requested_mode === 'auto_apply' ? 'Auto-apply' : 'Dry run'}</span><span><StatusLabel status={run.status} /></span><span>{formatDuration(run.started_at, run.finished_at)}</span><span className="tone-primary">{(run.counts.create ?? 0) + (run.counts.update ?? 0)} changes</span><span>{run.trigger === 'schedule' ? 'Schedule' : 'Manual'}</span><ArrowRight size={15} />
          </Link>)}
          <div className="table-hint"><CheckCircle2 size={16} /> Select a run to view details, plan, and diagnostics.</div>
        </div> : <EmptyState title="No runs yet" body="Preview a sync to create your first review plan and diagnostic timeline." />}
      </section>
    </section>

    <aside className="overview-rail inspector-rail">
      <section className="rail-section"><h2>System health</h2><div className="health-list">{data?.connections.checks.map((check) => <div className="health-row" key={check.key}><StatusIcon state={check.state} /><div><strong>{check.label}</strong><span>{check.summary}</span></div></div>)}</div><Link className="inline-link" to="/diagnostics">View diagnostics <ArrowRight size={16} /></Link></section>
      <section className="rail-section"><h2>Next scheduled run</h2>{data?.next_schedule ? <div className="next-schedule"><span className="round-icon"><CalendarDays size={22} /></span><div><strong>{formatDateTime(data.next_schedule.next_run_at)}</strong><span>{data.next_schedule.mode === 'auto_apply' ? 'Auto-apply safe changes' : 'Dry run'}</span><small>{data.next_schedule.mode === 'auto_apply' ? 'Safe changes apply automatically; issues wait for review.' : 'The next run will preview changes without making updates.'}</small></div></div> : <p className="muted-copy">No schedule is enabled for this course.</p>}<Link className="inline-link" to="/schedules">Manage schedule <ArrowRight size={16} /></Link></section>
      <section className="rail-section rail-callout"><span className="round-icon"><Check size={20} /></span><div><h2>Need deeper insight?</h2><p>Use Diagnostics to explore logs, extraction output, and configuration checks.</p><Link className="inline-link" to="/diagnostics">Go to Diagnostics <ArrowRight size={16} /></Link></div></section>
    </aside>
    {advancedOpen ? <Modal title="Advanced preview" onClose={() => setAdvancedOpen(false)} footer={<><Button variant="secondary" onClick={() => setAdvancedOpen(false)}>Cancel</Button><Button icon={Play} disabled={!course?.settings.enabled || isLoading} onClick={() => void previewSync(true)}>Start diagnostic preview</Button></>}><div className="advanced-preview-form"><label className="form-field"><span>Extraction mode override</span><select aria-label="Extraction mode override" value={selectedMode} onChange={(event) => setMode(event.target.value as ExtractionMode)}><option value="hybrid">Hybrid</option><option value="auto">Auto</option><option value="image">Image</option><option value="text">Text</option></select></label><label className="check-control"><input aria-label="Include past-due changes" type="checkbox" checked={includePast} onChange={(event) => setIncludePast(event.target.checked)} /><span><strong>Include past-due changes</strong><small>Only affects this manual preview. Review remains required before any write.</small></span></label><label className="form-field"><span>Rebase fixture week (optional)</span><input aria-label="Rebase fixture week (optional)" type="date" value={rebaseWeek} onChange={(event) => setRebaseWeek(event.target.value)} /><small>Choose a Monday. Rebasing is diagnostic-only and permanently disables Apply for the resulting preview.</small></label></div></Modal> : null}
  </div>
}
