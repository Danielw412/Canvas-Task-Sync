import { CalendarBlankIcon, CaretRightIcon, PlusIcon, TrashIcon, XCircleIcon } from '@phosphor-icons/react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import useSWR from 'swr'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, Field, FieldGroup, Modal, PageHeader, Sheet, SkeletonRows, Switch } from '../components/ui'
import { fetchJson, formatDateTime, formatRelative, humanize, mutateJson, useOverview } from '../lib/api'
import type { CourseView, Schedule, ScheduleOccurrence } from '../types'

interface ScheduleResponse { items: Schedule[]; occurrences: ScheduleOccurrence[] }
type ScheduleDraft = Omit<Schedule, 'id' | 'created_at' | 'updated_at' | 'next_run_at' | 'last_run_at' | 'last_result'> & { id?: number }

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export default function SchedulesPage() {
  const { selectedCourseId, toast } = useApp()
  const { data: overview } = useOverview(selectedCourseId)
  const { data, error, mutate } = useSWR<ScheduleResponse>('/api/v1/schedules', fetchJson)
  const [editing, setEditing] = useState<ScheduleDraft | null>(null)
  const [deleting, setDeleting] = useState<ScheduleDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const courses = overview?.courses ?? []
  const courseName = (id: string) => courses.find((course) => course.id === id)?.settings.name ?? id

  function newSchedule() {
    const course = selectedCourseId ?? courses[0]?.id ?? ''
    setEditing({
      name: '',
      course_id: course,
      weekdays: [0, 1, 2, 3, 4],
      local_time: '19:00',
      timezone: courses.find((item) => item.id === course)?.settings.timezone ?? 'America/New_York',
      mode: 'preview',
      enabled: true,
    })
  }

  async function toggle(schedule: Schedule, enabled: boolean) {
    setBusy(true)
    try {
      await mutateJson(`/api/v1/schedules/${schedule.id}/${enabled ? 'enable' : 'disable'}`)
      await mutate()
      toast(enabled ? `${schedule.name} is on.` : `${schedule.name} is paused.`, enabled ? 'success' : 'info')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Schedule could not change.', 'error')
    } finally { setBusy(false) }
  }

  async function save(draft: ScheduleDraft) {
    setBusy(true)
    try {
      const payload = { ...draft, local_time: draft.local_time.slice(0, 5) }
      await mutateJson<Schedule>(draft.id ? `/api/v1/schedules/${draft.id}` : '/api/v1/schedules', { method: draft.id ? 'PUT' : 'POST', body: payload })
      await mutate()
      setEditing(null)
      toast(draft.id ? 'Schedule changes saved.' : 'Schedule created.', 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Schedule could not be saved.', 'error')
    } finally { setBusy(false) }
  }

  async function remove() {
    if (!deleting?.id) return
    setBusy(true)
    try {
      await mutateJson(`/api/v1/schedules/${deleting.id}`, { method: 'DELETE' })
      await mutate()
      setDeleting(null)
      setEditing(null)
      toast('Schedule deleted.', 'warning')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Schedule could not be deleted.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <div className="surface"><EmptyState icon={XCircleIcon} title="Schedules could not load" body={error.message} /></div>

  return <div className="page--schedules">
    <PageHeader
      title="Schedules"
      description="Sync courses automatically while the server is running. A time missed while it was off is skipped and noted below."
      actions={<Button icon={PlusIcon} disabled={!courses.length} onClick={newSchedule}>New schedule</Button>}
    />

    {!data ? <SkeletonRows rows={2} label="Loading schedules" /> : data.items.length ? <div className="surface">
      {data.items.map((schedule) => <div
        className={`row row--interactive schedule-row${schedule.enabled ? '' : ' is-paused'}`}
        key={schedule.id}
        onClick={() => setEditing(toDraft(schedule))}
      >
        <Switch checked={schedule.enabled} label={`${schedule.enabled ? 'Pause' : 'Turn on'} ${schedule.name}`} disabled={busy} onChange={(value) => void toggle(schedule, value)} />
        <span className="schedule-row__name">
          <button type="button" className="row-button truncate" onClick={(event) => { event.stopPropagation(); setEditing(toDraft(schedule)) }}>{schedule.name}</button>
          <span className="row-meta truncate">{courseName(schedule.course_id)}</span>
        </span>
        <span className="schedule-row__when">{weekdayLabel(schedule.weekdays)} at {timeLabel(schedule.local_time)}</span>
        <span>{schedule.mode === 'auto_apply' ? <span className="badge badge--info">Applies safe changes</span> : <span className="badge">Preview only</span>}</span>
        <span className="schedule-row__next">{schedule.enabled ? schedule.next_run_at ? `Next ${formatRelative(schedule.next_run_at)}` : 'Not scheduled' : 'Paused'}</span>
        <CaretRightIcon className="row-chevron" size={15} aria-hidden />
      </div>)}
    </div> : <div className="surface"><EmptyState icon={CalendarBlankIcon} title="No schedules yet" body="Run a preview or a safe sync on the days and time you choose." action={<Button icon={PlusIcon} disabled={!courses.length} onClick={newSchedule}>New schedule</Button>} /></div>}

    <section className="section" aria-labelledby="activity-heading">
      <div className="section-head"><h2 id="activity-heading">Recent activity</h2></div>
      {data?.occurrences.length ? <div className="surface">
        {data.occurrences.map((occurrence) => {
          const name = data.items.find((schedule) => schedule.id === occurrence.schedule_id)?.name ?? `Schedule #${occurrence.schedule_id}`
          const content = <>
            <span className="subtle num">{formatDateTime(occurrence.scheduled_for)}</span>
            <span className="row-title truncate">{name}</span>
            <span><OccurrenceBadge status={occurrence.status} /></span>
            <span className="row-meta truncate">{occurrence.details}</span>
            {occurrence.run_id ? <CaretRightIcon className="row-chevron" size={15} aria-hidden /> : <span />}
          </>
          return occurrence.run_id
            ? <Link className="row row--interactive activity-row" to={`/runs/${occurrence.run_id}`} key={occurrence.id} aria-label={`${name}, open run`}>{content}</Link>
            : <div className="row activity-row" key={occurrence.id}>{content}</div>
        })}
      </div> : <p className="subtle">Each scheduled time appears here once it runs or is missed.</p>}
    </section>

    {editing ? <ScheduleEditor
      draft={editing}
      courses={courses}
      busy={busy}
      onClose={() => setEditing(null)}
      onSave={(draft) => void save(draft)}
      onDelete={() => setDeleting(editing)}
    /> : null}
    {deleting ? <Modal
      title={`Delete ${deleting.name || 'this schedule'}?`}
      onClose={() => { if (!busy) setDeleting(null) }}
      footer={<><Button variant="secondary" disabled={busy} onClick={() => setDeleting(null)}>Cancel</Button><Button variant="danger-solid" icon={TrashIcon} loading={busy} onClick={() => void remove()}>Delete schedule</Button></>}
    ><p>It stops running right away. Past runs stay in your run history.</p></Modal> : null}
  </div>
}

function OccurrenceBadge({ status }: { status: string }) {
  if (status === 'queued') return <span className="badge badge--success">Started</span>
  if (status === 'missed') return <span className="badge badge--warning">Missed</span>
  if (status === 'failed' || status === 'error') return <span className="badge badge--danger">{humanize(status)}</span>
  return <span className="badge">{humanize(status)}</span>
}

function ScheduleEditor({ draft: initial, courses, busy, onClose, onSave, onDelete }: {
  draft: ScheduleDraft
  courses: CourseView[]
  busy: boolean
  onClose: () => void
  onSave: (draft: ScheduleDraft) => void
  onDelete: () => void
}) {
  const [draft, setDraft] = useState(initial)
  const update = <K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) => setDraft((current) => ({ ...current, [key]: value }))
  const toggleDay = (index: number) => update('weekdays', draft.weekdays.includes(index) ? draft.weekdays.filter((value) => value !== index) : [...draft.weekdays, index].sort())

  return <Sheet
    title={draft.id ? 'Edit schedule' : 'New schedule'}
    onClose={onClose}
    footer={<>
      {draft.id ? <Button variant="danger" icon={TrashIcon} disabled={busy} onClick={onDelete}>Delete</Button> : null}
      <span className="sheet__footer-spacer" />
      <Button variant="ghost" onClick={onClose}>Cancel</Button>
      <Button loading={busy} disabled={!draft.name || !draft.course_id || !draft.weekdays.length} onClick={() => onSave(draft)}>Save schedule</Button>
    </>}
  >
    <Field label="Name"><input className="control" value={draft.name} onChange={(event) => update('name', event.target.value)} placeholder="Weekday evening sync" /></Field>
    <Field label="Course">
      <select className="control" value={draft.course_id} onChange={(event) => update('course_id', event.target.value)}>
        {courses.map((course) => <option key={course.id} value={course.id}>{course.settings.name}</option>)}
      </select>
    </Field>
    <FieldGroup label="Days" help={<span className="inline-actions">Quick picks: <button type="button" className="text-link" onClick={() => update('weekdays', [0, 1, 2, 3, 4])}>Weekdays</button><button type="button" className="text-link" onClick={() => update('weekdays', [0, 1, 2, 3, 4, 5, 6])}>Every day</button></span>}>
      <div className="chips">
        {DAY_LABELS.map((day, index) => <button type="button" className="chip" key={day} aria-pressed={draft.weekdays.includes(index)} onClick={() => toggleDay(index)}>{day}</button>)}
      </div>
    </FieldGroup>
    <div className="form-grid">
      <Field label="Time"><input className="control" type="time" value={draft.local_time.slice(0, 5)} onChange={(event) => update('local_time', event.target.value)} /></Field>
      <Field label="Timezone"><input className="control" value={draft.timezone} onChange={(event) => update('timezone', event.target.value)} /></Field>
    </div>
    <FieldGroup label="When it runs">
      <div className="choice-grid choice-grid--2" role="radiogroup" aria-label="Run behavior">
        <label className={`choice${draft.mode === 'preview' ? ' is-selected' : ''}`}>
          <input type="radio" name="schedule-mode" checked={draft.mode === 'preview'} onChange={() => update('mode', 'preview')} />
          <span className="choice__title">Preview only</span>
          <span className="choice__desc">Builds a plan for you to review. Nothing is written.</span>
        </label>
        <label className={`choice${draft.mode === 'auto_apply' ? ' is-selected' : ''}`}>
          <input type="radio" name="schedule-mode" checked={draft.mode === 'auto_apply'} onChange={() => update('mode', 'auto_apply')} />
          <span className="choice__title">Apply safe changes</span>
          <span className="choice__desc">Creates and updates tasks. Uncertain, missing, and past-due items wait for you. Nothing is deleted.</span>
        </label>
      </div>
    </FieldGroup>
    <div className="setting setting--inline">
      <div className="setting__text"><span className="setting__title">Schedule is on</span><span className="setting__desc">Turn off to pause it without losing its settings.</span></div>
      <div className="setting__control"><Switch checked={draft.enabled} label="Schedule enabled" onChange={(value) => update('enabled', value)} /></div>
    </div>
  </Sheet>
}

function toDraft(schedule: Schedule): ScheduleDraft {
  return { id: schedule.id, name: schedule.name, course_id: schedule.course_id, weekdays: [...schedule.weekdays], local_time: schedule.local_time, timezone: schedule.timezone, mode: schedule.mode, enabled: schedule.enabled }
}

function weekdayLabel(values: number[]) {
  const key = [...values].sort().join(',')
  if (key === '0,1,2,3,4') return 'Weekdays'
  if (key === '0,1,2,3,4,5,6') return 'Every day'
  if (key === '5,6') return 'Weekends'
  return values.map((value) => DAY_LABELS[value]).join(', ')
}

function timeLabel(value: string) {
  const [hours, minutes] = value.split(':').map(Number)
  return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date(2026, 0, 1, hours, minutes))
}
