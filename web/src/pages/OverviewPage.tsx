import {
  ArrowRightIcon,
  ArrowsClockwiseIcon,
  BooksIcon,
  CalendarBlankIcon,
  ClockCounterClockwiseIcon,
  DotsThreeIcon,
  EyeIcon,
  GearSixIcon,
  WarningCircleIcon,
  XCircleIcon,
} from '@phosphor-icons/react'
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import { useApp } from '../components/AppContext'
import {
  Button,
  Disclosure,
  EmptyState,
  Field,
  Menu,
  Modal,
  Notice,
  PageHeader,
  RunStatusBadge,
  Segmented,
  SkeletonRows,
} from '../components/ui'
import {
  agendaWeeks,
  attentionTotal,
  changeSummary,
  fetchJson,
  formatRelative,
  isActiveRun,
  mutateJson,
  revalidateOverview,
  stageLabels,
  useOverview,
  wakeExtensionCaptureQueue,
} from '../lib/api'
import type {
  AcquisitionStrategy,
  CourseView,
  ExtractionMode,
  RunSummary,
  Schedule,
  WeekSelection,
} from '../types'

interface AttentionItem {
  run: RunSummary
  tone: 'warning' | 'danger' | 'info'
  title: string
  detail: string
  action: string
}

function attentionFor(run: RunSummary, courseName: string): AttentionItem | null {
  const flagged = attentionTotal(run.counts)
  if (run.status === 'awaiting_approval') {
    return { run, tone: 'info', title: `${courseName} preview is ready to review`, detail: [changeSummary(run), flagged ? `${flagged} need attention` : ''].filter(Boolean).join(', ') || 'No changes were found.', action: 'Review' }
  }
  if (run.status === 'review_needed') {
    return { run, tone: 'warning', title: `${courseName} has ${flagged || 'some'} ${flagged === 1 ? 'item' : 'items'} to check`, detail: 'Safe changes were applied. Anything uncertain was left for you.', action: 'Review' }
  }
  if (run.status === 'stale') {
    return { run, tone: 'warning', title: `${courseName} preview is out of date`, detail: 'The source or Google Tasks changed after it was built. Start a new preview.', action: 'Open' }
  }
  if (run.status === 'failed' || run.status === 'failed_partial') {
    return { run, tone: 'danger', title: `${courseName} sync ${run.status === 'failed' ? 'failed' : 'partly applied'}`, detail: run.error_summary ?? 'Open the run to see what happened.', action: 'View' }
  }
  return null
}

function sourceLine(course: CourseView) {
  const { settings } = course
  if (settings.canvas_course_id) return `Canvas course ${settings.canvas_course_id}`
  if (settings.source.type === 'browser') return 'Chrome capture'
  if (settings.source.type === 'google_slides') return 'Google Slides'
  return 'No agenda source'
}

export default function OverviewPage() {
  const { selectedCourseId, setSelectedCourseId, toast } = useApp()
  const { data, error } = useOverview(selectedCourseId)
  const { data: runsData, mutate: mutateRuns } = useSWR<RunSummary[]>('/api/v1/runs?limit=60', fetchJson, {
    refreshInterval: (current) => Array.isArray(current) && current.some((run) => isActiveRun(run.status)) ? 2_000 : 0,
  })
  const { data: scheduleData } = useSWR<{ items: Schedule[] }>('/api/v1/schedules', fetchJson)
  const navigate = useNavigate()
  const [week, setWeek] = useState<WeekSelection>('this_week')
  const [startingAll, setStartingAll] = useState(false)
  const [startingCourse, setStartingCourse] = useState<string | null>(null)
  const [previewCourse, setPreviewCourse] = useState<CourseView | null>(null)

  const courses = useMemo(() => data?.courses ?? [], [data?.courses])
  const runs = useMemo(() => Array.isArray(runsData) ? runsData : [], [runsData])
  const timezone = courses.find((course) => course.id === data?.selected_course_id)?.settings.timezone ?? courses[0]?.settings.timezone
  const weeks = agendaWeeks(timezone)
  const selectedWeek = weeks.find((item) => item.value === week) ?? weeks[1]

  const latestByCourse = useMemo(() => {
    const latest = new Map<string, RunSummary>()
    for (const run of runs) {
      if (run.requested_mode === 'health') continue
      const current = latest.get(run.course_id)
      if (!current || new Date(run.created_at) > new Date(current.created_at)) latest.set(run.course_id, run)
    }
    return latest
  }, [runs])

  const attention = useMemo(() => courses
    .map((course) => {
      const run = latestByCourse.get(course.id)
      return run ? attentionFor(run, course.settings.name) : null
    })
    .filter((item): item is AttentionItem => item !== null), [courses, latestByCourse])

  const nextSchedule = useMemo(() => (Array.isArray(scheduleData?.items) ? scheduleData.items : [])
    .filter((schedule) => schedule.enabled && schedule.next_run_at)
    .sort((a, b) => new Date(a.next_run_at!).getTime() - new Date(b.next_run_at!).getTime())[0], [scheduleData])

  async function syncCourse(course: CourseView) {
    setStartingCourse(course.id)
    setSelectedCourseId(course.id)
    try {
      const result = await mutateJson<{ run_id: number; capture_request_id?: string | null }>('/api/v1/runs', {
        body: {
          course_id: course.id,
          mode: 'auto_apply',
          week_selection: week,
          acquisition_strategy: 'auto',
          extraction_mode: course.settings.source.extraction.mode,
          include_past: false,
        },
      })
      if (result.capture_request_id) wakeExtensionCaptureQueue()
      await Promise.all([mutateRuns(), revalidateOverview()])
      toast(`Syncing ${course.settings.name}.`, 'info')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Sync could not be started.', 'error')
    } finally {
      setStartingCourse(null)
    }
  }

  async function syncAllCourses() {
    setStartingAll(true)
    try {
      const result = await mutateJson<{ run_ids: number[]; capture_request_ids: string[] }>('/api/v1/runs/all', {
        body: { include_past: false, mode: 'auto_apply', week_selection: week, acquisition_strategy: 'auto' },
      })
      if (result.capture_request_ids.length) wakeExtensionCaptureQueue()
      await Promise.all([mutateRuns(), revalidateOverview()])
      toast(`Started syncing ${result.run_ids.length} ${result.run_ids.length === 1 ? 'course' : 'courses'}.`, 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Courses could not be synced.', 'error')
    } finally {
      setStartingAll(false)
    }
  }

  if (error) return <EmptyState icon={XCircleIcon} title="Overview could not load" body={error.message} />
  if (!data) return <div><PageHeader title="Overview" description="Loading your courses." /><SkeletonRows rows={4} label="Loading courses" /></div>

  const { connections } = data
  const connected = connections.google_authorized && connections.extraction_ready
  const enabledCount = courses.filter((course) => course.settings.enabled).length
  const lastSync = [...latestByCourse.values()].map((run) => run.finished_at ?? run.created_at).sort().at(-1)
  const title = !connected
    ? 'Finish setup to start syncing'
    : attention.length
      ? `${attention.length} ${attention.length === 1 ? 'course needs' : 'courses need'} a look`
      : 'Everything is ready to sync'
  const description = courses.length
    ? `${enabledCount} of ${courses.length} ${courses.length === 1 ? 'course' : 'courses'} enabled. ${lastSync ? `Last synced ${formatRelative(lastSync)}.` : 'Nothing synced yet.'}`
    : 'Add a course to turn its Canvas agenda into Google Tasks.'

  return <div className="page--overview">
    <PageHeader
      title={title}
      description={description}
      actions={courses.length ? <div className="sync-actions">
        <div className="week-picker">
          <Segmented
            label="Agenda week"
            value={week}
            onChange={setWeek}
            options={weeks.map((item) => ({ value: item.value, label: item.name, ariaLabel: `${item.name}, ${item.range}` }))}
          />
          <span className="week-picker__range">{selectedWeek?.range}</span>
        </div>
        <Button icon={ArrowsClockwiseIcon} loading={startingAll} disabled={!enabledCount} onClick={() => void syncAllCourses()}>Sync all courses</Button>
      </div> : null}
    />

    {!connected ? <div className="overview-notices">
      <Notice
        tone="warning"
        title={!connections.google_authorized ? 'Google Tasks is not connected' : 'The extraction agent is not ready'}
        actions={<Button variant="secondary" size="sm" icon={GearSixIcon} onClick={() => navigate(`/settings?section=${connections.google_authorized ? 'ai' : 'google'}`)}>Open settings</Button>}
      >{!connections.google_authorized ? 'Authorize Google so synced tasks can be written to your lists.' : `${connections.extraction_label} needs attention before courses can be read.`}</Notice>
    </div> : null}

    {attention.length ? <section className="section" aria-labelledby="attention-heading">
      <div className="section-head"><h2 id="attention-heading">Needs your attention</h2></div>
      <div className="surface">
        {attention.map((item) => <Link to={`/runs/${item.run.id}`} className="row row--interactive attention-row" key={item.run.id}>
          {item.tone === 'danger' ? <XCircleIcon className="tone-danger" size={20} weight="fill" aria-hidden /> : item.tone === 'warning' ? <WarningCircleIcon className="tone-warning" size={20} weight="fill" aria-hidden /> : <EyeIcon className="tone-info" size={20} aria-hidden />}
          <span className="attention-row__text"><span className="row-title">{item.title}</span><span className="row-meta">{item.detail}</span></span>
          <span className="text-link">{item.action}<ArrowRightIcon size={14} aria-hidden /></span>
        </Link>)}
      </div>
    </section> : null}

    <section className="section" aria-labelledby="courses-heading">
      <div className="section-head">
        <h2 id="courses-heading">Courses</h2>
        <div className="section-head__aside"><Link className="text-link" to="/courses">Manage courses<ArrowRightIcon size={14} aria-hidden /></Link></div>
      </div>
      {courses.length ? <div className="surface">
        {courses.map((course) => {
          const run = latestByCourse.get(course.id)
          const running = run ? isActiveRun(run.status) : false
          const enabled = course.settings.enabled
          return <div className={`row course-row${enabled ? '' : ' course-row--disabled'}`} key={course.id}>
            <div className="course-row__name">
              <Link to={`/courses?course=${encodeURIComponent(course.id)}`}><span className="row-title">{course.settings.name}</span></Link>
              <span className={`row-meta truncate${course.readiness === 'error' ? ' tone-danger' : course.readiness === 'warning' ? ' tone-warning' : ''}`}>
                {!enabled ? 'Disabled' : course.readiness === 'healthy' ? sourceLine(course) : course.readiness_message}
              </span>
            </div>
            <div className="course-row__status">
              {run ? <Link to={`/runs/${run.id}`} title="Open this run">
                <RunStatusBadge status={run.status} />
                <span className="row-meta truncate">{running ? `${stageLabels[run.stage]}…` : [formatRelative(run.finished_at ?? run.created_at), changeSummary(run)].filter(Boolean).join(', ')}</span>
              </Link> : <span className="row-meta">Not synced yet</span>}
            </div>
            <div className="course-row__actions">
              <Button
                variant="secondary"
                size="sm"
                icon={ArrowsClockwiseIcon}
                aria-label={`Sync ${course.settings.name}`}
                loading={startingCourse === course.id}
                disabled={!enabled || running || startingAll}
                onClick={() => void syncCourse(course)}
              >Sync</Button>
              <Menu
                label={`More actions for ${course.settings.name}`}
                trigger={<DotsThreeIcon size={20} weight="bold" aria-hidden />}
                items={[
                  { label: 'Preview changes…', icon: EyeIcon, disabled: !enabled, onSelect: () => { setSelectedCourseId(course.id); setPreviewCourse(course) } },
                  { label: 'Run history', icon: ClockCounterClockwiseIcon, onSelect: () => navigate(`/runs?course=${encodeURIComponent(course.id)}`) },
                  { label: 'Course settings', icon: GearSixIcon, onSelect: () => navigate(`/courses?course=${encodeURIComponent(course.id)}`) },
                ]}
              />
            </div>
          </div>
        })}
      </div> : <div className="surface"><EmptyState icon={BooksIcon} title="Add your first course" body="Connect a Canvas course and choose the Google Tasks lists its work should go to." action={<Button icon={BooksIcon} onClick={() => navigate('/courses?new=1')}>Add course</Button>} /></div>}
      {courses.length ? <p className="footnote">
        <CalendarBlankIcon size={15} aria-hidden />
        {nextSchedule
          ? <span>Next scheduled run: {nextSchedule.name}, {formatRelative(nextSchedule.next_run_at)}. <Link to="/schedules">Manage schedules</Link></span>
          : <span>No schedules are running. <Link to="/schedules">Set one up</Link> to sync automatically.</span>}
      </p> : null}
    </section>

    {previewCourse ? <PreviewDialog
      course={previewCourse}
      week={week}
      setWeek={setWeek}
      weeks={weeks}
      onClose={() => setPreviewCourse(null)}
      onStarted={(runId) => { setPreviewCourse(null); navigate(`/runs/${runId}`) }}
    /> : null}
  </div>
}

function PreviewDialog({ course, week, setWeek, weeks, onClose, onStarted }: {
  course: CourseView
  week: WeekSelection
  setWeek: (value: WeekSelection) => void
  weeks: ReturnType<typeof agendaWeeks>
  onClose: () => void
  onStarted: (runId: number) => void
}) {
  const { toast } = useApp()
  const hasFallback = course.settings.source.type !== 'none'
  const [strategy, setStrategy] = useState<AcquisitionStrategy>('auto')
  const [mode, setMode] = useState<ExtractionMode>(course.settings.source.extraction.mode)
  const [includePast, setIncludePast] = useState(false)
  const [rebaseWeek, setRebaseWeek] = useState('')
  const [starting, setStarting] = useState(false)

  async function start() {
    if (rebaseWeek && new Date(`${rebaseWeek}T12:00:00`).getDay() !== 1) {
      toast('The test week must begin on a Monday.', 'warning')
      return
    }
    setStarting(true)
    try {
      const result = await mutateJson<{ run_id: number; capture_request_id?: string | null }>('/api/v1/runs', {
        body: {
          course_id: course.id,
          mode: 'preview',
          week_selection: week,
          acquisition_strategy: strategy,
          extraction_mode: mode,
          include_past: includePast,
          test_rebase_week: rebaseWeek || undefined,
        },
      })
      if (result.capture_request_id) wakeExtensionCaptureQueue()
      await revalidateOverview()
      onStarted(result.run_id)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Preview could not be started.', 'error')
      setStarting(false)
    }
  }

  return <Modal
    title="Preview changes"
    onClose={onClose}
    footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button icon={EyeIcon} loading={starting} onClick={() => void start()}>Start preview</Button></>}
  >
    <p>Builds the plan for <strong>{course.settings.name}</strong> without writing anything. You review it, then choose whether to apply.</p>
    <div className="field">
      <span className="field__label">Week</span>
      <Segmented label="Preview week" value={week} onChange={setWeek} options={weeks.map((item) => ({ value: item.value, label: item.name, ariaLabel: `${item.name}, ${item.range}` }))} />
      <small className="field__help">{weeks.find((item) => item.value === week)?.range}</small>
    </div>
    <Disclosure title="Advanced options" hint="For troubleshooting">
      <div className="form-stack">
        <Field label="Agenda source">
          <select className="control" aria-label="Agenda source" value={strategy} onChange={(event) => setStrategy(event.target.value as AcquisitionStrategy)}>
            <option value="auto">{hasFallback ? 'Canvas first, then the fallback' : 'Canvas API (no fallback configured)'}</option>
            <option value="canvas_api" disabled={!course.settings.canvas_course_id}>Canvas API only</option>
            {hasFallback ? <option value="configured_source">{course.settings.source.type === 'browser' ? 'Chrome capture only' : 'Google Slides only'}</option> : null}
          </select>
        </Field>
        <Field label="Extraction mode">
          <select className="control" aria-label="Extraction mode override" value={mode} onChange={(event) => setMode(event.target.value as ExtractionMode)}>
            <option value="hybrid">Hybrid</option><option value="auto">Auto</option><option value="image">Image</option><option value="text">Text</option>
          </select>
        </Field>
        <label className="check">
          <input aria-label="Include past-due changes" type="checkbox" checked={includePast} onChange={(event) => setIncludePast(event.target.checked)} />
          <span className="check__text"><strong>Include past-due changes</strong><small>Only for this preview. You still approve every write.</small></span>
        </label>
        <Field label="Rebase fixture week (optional)" help="Pick a Monday. A rebased preview can't be applied.">
          <input className="control" aria-label="Rebase fixture week (optional)" type="date" value={rebaseWeek} onChange={(event) => setRebaseWeek(event.target.value)} />
        </Field>
      </div>
    </Disclosure>
  </Modal>
}
