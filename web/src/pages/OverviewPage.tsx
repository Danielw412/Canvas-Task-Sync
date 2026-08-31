import {
  ArrowRight,
  CalendarDays,
  CheckCircle2,
  Clock3,
  FileSliders,
  Play,
  Presentation,
  SlidersHorizontal,
  Sparkles,
  Target,
} from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { mutate } from 'swr'
import { agendaWeekOptions, formatDateTime, mutateJson, useOverview } from '../lib/api'
import type { AcquisitionStrategy, ExtractionMode, RunSummary, WeekSelection } from '../types'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, Modal, StatusLabel } from '../components/ui'

const planKinds = [
  { key: 'create', label: 'Create', tone: 'success' },
  { key: 'update', label: 'Update', tone: 'primary' },
  { key: 'unchanged', label: 'Unchanged', tone: 'muted' },
  { key: 'attention', label: 'Attention', tone: 'warning' },
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
  const [weekSelection, setWeekSelection] = useState<WeekSelection>('this_week')
  const [acquisitionStrategy, setAcquisitionStrategy] = useState<AcquisitionStrategy>('auto')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [includePast, setIncludePast] = useState(false)
  const [rebaseWeek, setRebaseWeek] = useState('')
  const [startingAll, setStartingAll] = useState(false)
  const selectedMode = mode || course?.settings.source.extraction.mode || 'hybrid'
  const selectedAcquisitionStrategy = course?.settings.source.type === 'none' && acquisitionStrategy === 'configured_source' ? 'auto' : acquisitionStrategy
  const weekOptions = agendaWeekOptions(course?.settings.timezone)

  async function syncCourse(advanced = false) {
    if (!course) return
    if (advanced && rebaseWeek && new Date(`${rebaseWeek}T12:00:00`).getDay() !== 1) {
      toast('The test week must begin on a Monday.', 'warning')
      return
    }
    try {
      const result = await mutateJson<{ run_id: number; capture_request_id?: string | null }>('/api/v1/runs', {
        body: {
          course_id: course.id,
          mode: advanced ? 'preview' : 'auto_apply',
          week_selection: weekSelection,
          acquisition_strategy: selectedAcquisitionStrategy,
          extraction_mode: selectedMode,
          include_past: advanced ? includePast : false,
          test_rebase_week: advanced && rebaseWeek ? rebaseWeek : undefined,
        },
      })
      if (result.capture_request_id) wakeExtensionCaptureQueue()
      setAdvancedOpen(false)
      await mutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
      navigate(`/runs/${result.run_id}`)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Sync could not be started.', 'error')
    }
  }

  async function syncAllCourses() {
    setStartingAll(true)
    try {
      const result = await mutateJson<{ run_ids: number[]; capture_request_ids: string[] }>('/api/v1/runs/all', {
        body: {
          include_past: false,
          mode: 'auto_apply',
          week_selection: weekSelection,
          acquisition_strategy: selectedAcquisitionStrategy,
        },
      })
      if (result.capture_request_ids.length) wakeExtensionCaptureQueue()
      await mutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
      toast(`Started syncing ${result.run_ids.length} courses in parallel.`, 'success')
      navigate('/runs')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Courses could not be synced.', 'error')
    } finally {
      setStartingAll(false)
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
        <div><h1>{connected ? 'Everything is ready to sync' : 'Finish setup to start syncing'}</h1><p>Sync safe changes automatically and review anything that still needs attention.</p></div>
        <div className="overview-actions">
          <label className="select-control week-select-control"><span className="sr-only">Agenda week</span><select aria-label="Agenda week" value={weekSelection} onChange={(event) => setWeekSelection(event.target.value as WeekSelection)}>{weekOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>
          <Button icon={Play} onClick={() => void syncAllCourses()} disabled={isLoading || startingAll}>{startingAll ? 'Starting all…' : 'Sync all courses'}</Button>
          <Button variant="secondary" icon={Play} onClick={() => void syncCourse()} disabled={!course?.settings.enabled || isLoading}>Sync selected course</Button>
          <Button variant="ghost" icon={SlidersHorizontal} className="advanced-preview-button" aria-label="Advanced preview" title="Advanced preview" onClick={() => setAdvancedOpen(true)} />
        </div>
      </header>

      <section className="source-workspace panel">
        <div className="source-summary">
          <div className="source-summary__item"><span className="source-icon source-icon--slides"><Presentation size={19} /></span><div><span>Source</span><strong>{course?.settings.canvas_course_id ? `Canvas course ${course.settings.canvas_course_id}` : course?.settings.source.type === 'browser' ? 'Chrome capture' : 'Google Slides API'}</strong></div></div>
          <div className="source-summary__item"><span className="source-icon source-icon--tasks"><Target size={19} /></span><div><span>Task lists</span><strong>{course?.settings.task_list ?? 'Assignments'} / {course?.settings.assessment_task_list ?? 'Tests'}</strong></div></div>
          <div className="source-summary__item"><Clock3 size={20} /><div><span>Last checked</span><strong>{latest ? formatDateTime(latest.finished_at ?? latest.created_at) : 'Not checked yet'}</strong></div></div>
        </div>
        <div className="mobile-health-summary">
          <div><span className="source-icon source-icon--slides"><Presentation size={19} /></span><strong>Source</strong><small className={connected ? 'tone-success' : 'tone-warning'}>{connected ? 'Ready' : 'Check setup'}</small></div>
          <div><span className="source-icon source-icon--gemini"><Sparkles size={19} /></span><strong>Gemini</strong><small className={data?.connections.gemini_configured ? 'tone-success' : 'tone-warning'}>{data?.connections.gemini_configured ? 'Connected' : 'Missing'}</small></div>
          <div><span className="source-icon source-icon--tasks"><Target size={19} /></span><strong>Google Tasks</strong><small className={data?.connections.google_authorized ? 'tone-success' : 'tone-warning'}>{data?.connections.google_authorized ? 'Accessible' : 'Check setup'}</small></div>
          <p>{connected ? 'All systems healthy' : 'Setup needs attention'}</p>
        </div>
        <div className="flow-steps" aria-label="Sync stages">
          {flowStages.map(({ label, icon: Icon }, index) => <div className="flow-step" key={label}><div className="flow-step__line">{index ? <span /> : null}<i className={connected ? 'is-healthy' : ''}><Icon size={17} /></i>{index < 3 ? <span /> : null}</div><strong>{label}</strong><small className={connected ? 'tone-success' : 'tone-muted'}>{connected ? 'Ready' : 'Waiting'}</small></div>)}
        </div>
      </section>

      <section className="plan-band panel" aria-label="Latest plan summary">
        {planKinds.map((item) => <div className="plan-metric" key={item.key}><span className={`plan-metric__value tone-${item.tone}`}>{values[item.key]}</span><div><strong>{item.label}</strong></div></div>)}
        {latest ? <Link className="inline-link plan-band__link" to={`/runs/${latest.id}`}>Review latest plan <ArrowRight size={17} /></Link> : <span className="plan-band__empty">No preview yet</span>}
      </section>

      <section className="recent-section">
        <div className="section-heading"><h2>Recent runs</h2><Link className="inline-link" to="/runs">View all runs <ArrowRight size={17} /></Link></div>
        {data?.recent_runs.length ? <div className="table-frame recent-runs-table">
          <div className="data-table data-table--runs data-table__header"><span>Started</span><span>Course</span><span>Result</span><span>Changes</span><span /></div>
          {data.recent_runs.map((run) => <Link to={`/runs/${run.id}`} className="data-table data-table--runs data-row" key={run.id}>
            <span>{formatDateTime(run.started_at ?? run.created_at)}</span><span>{run.course_name ?? run.course_id}</span><span><StatusLabel status={run.status} /></span><span className="tone-primary">{(run.counts.create ?? 0) + (run.counts.update ?? 0) + (run.counts.notes_cleanup ?? 0)} changes</span><ArrowRight size={15} />
          </Link>)}
          <div className="table-hint"><CheckCircle2 size={16} /> Select a run to review its plan.</div>
        </div> : <EmptyState title="No runs yet" body="Start a sync to create your first run and diagnostic timeline." />}
      </section>
    </section>

    {advancedOpen ? <Modal title="Advanced preview" onClose={() => setAdvancedOpen(false)} footer={<><Button variant="secondary" onClick={() => setAdvancedOpen(false)}>Cancel</Button><Button icon={Play} disabled={!course?.settings.enabled || isLoading} onClick={() => void syncCourse(true)}>Start diagnostic preview</Button></>}><div className="advanced-preview-form"><label className="form-field"><span>Agenda source</span><select aria-label="Agenda source" value={selectedAcquisitionStrategy} onChange={(event) => setAcquisitionStrategy(event.target.value as AcquisitionStrategy)}><option value="auto">{course?.settings.source.type === 'none' ? 'Canvas API, no fallback' : 'Canvas first, automatic fallback'}</option><option value="canvas_api" disabled={!course?.settings.canvas_course_id}>Canvas API only</option>{course?.settings.source.type !== 'none' ? <option value="configured_source">{course?.settings.source.type === 'browser' ? 'Chrome capture only' : 'Configured API source only'}</option> : null}</select></label><label className="form-field"><span>Extraction mode</span><select aria-label="Extraction mode override" value={selectedMode} onChange={(event) => setMode(event.target.value as ExtractionMode)}><option value="hybrid">Hybrid</option><option value="auto">Auto</option><option value="image">Image</option><option value="text">Text</option></select></label><label className="check-control"><input aria-label="Include past-due changes" type="checkbox" checked={includePast} onChange={(event) => setIncludePast(event.target.checked)} /><span><strong>Include past-due changes</strong><small>Only affects this preview. Review is still required.</small></span></label><label className="form-field"><span>Rebase fixture week (optional)</span><input aria-label="Rebase fixture week (optional)" type="date" value={rebaseWeek} onChange={(event) => setRebaseWeek(event.target.value)} /><small>Choose a Monday. Rebasing disables Apply for this preview.</small></label></div></Modal> : null}
  </div>
}

function wakeExtensionCaptureQueue() {
  window.postMessage(
    { source: 'canvas-task-sync-web', type: 'capture-requested' },
    window.location.origin,
  )
}
