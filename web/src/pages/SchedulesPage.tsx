import { AlertTriangle, ArrowRight, CalendarDays, Check, Clock3, Info, Plus, Save } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import useSWR from 'swr'
import { useApp } from '../components/AppContext'
import { Button, CheckRow, EmptyState } from '../components/ui'
import { fetchJson, formatDateTime, mutateJson, useOverview } from '../lib/api'
import type { Schedule, ScheduleOccurrence } from '../types'

interface ScheduleResponse { items: Schedule[]; occurrences: ScheduleOccurrence[] }
type ScheduleDraft = Omit<Schedule, 'id' | 'created_at' | 'updated_at' | 'next_run_at' | 'last_run_at' | 'last_result'> & { id?: number }

export default function SchedulesPage() {
  const { selectedCourseId, toast } = useApp()
  const { data: overview } = useOverview(selectedCourseId)
  const { data, error, mutate } = useSWR<ScheduleResponse>('/api/v1/schedules', fetchJson)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const selected = data?.items.find((schedule) => schedule.id === selectedId) ?? data?.items[0]
  const [draft, setDraft] = useState<ScheduleDraft | null>(null)
  const active = draft ?? (selected ? toDraft(selected) : null)
  const [edited, setEdited] = useState(false)
  const [busy, setBusy] = useState(false)

  function choose(schedule: Schedule) {
    setSelectedId(schedule.id)
    setDraft(null)
    setEdited(false)
  }

  function newSchedule() {
    const course = selectedCourseId ?? overview?.courses[0]?.id ?? ''
    setDraft({ name: '', course_id: course, weekdays: [0, 1, 2, 3, 4], local_time: '19:00', timezone: overview?.courses.find((item) => item.id === course)?.settings.timezone ?? 'America/New_York', mode: 'preview', enabled: true })
    setSelectedId(null)
    setEdited(true)
  }

  function update<K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) {
    if (!active) return
    setDraft({ ...active, [key]: value })
    setEdited(true)
  }

  async function save() {
    if (!active) return
    setBusy(true)
    try {
      const payload = { ...active, local_time: active.local_time.slice(0, 5) }
      const saved = await mutateJson<Schedule>(active.id ? `/api/v1/schedules/${active.id}` : '/api/v1/schedules', { method: active.id ? 'PUT' : 'POST', body: payload })
      await mutate()
      setSelectedId(saved.id)
      setDraft(null)
      setEdited(false)
      toast(active.id ? 'Schedule changes saved.' : 'Schedule created.', 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Schedule could not be saved.', 'error')
    } finally { setBusy(false) }
  }

  async function remove() {
    if (!active?.id || !window.confirm('Delete this schedule? Existing run history is kept.')) return
    setBusy(true)
    try {
      await mutateJson(`/api/v1/schedules/${active.id}`, { method: 'DELETE' })
      await mutate()
      setSelectedId(null)
      setDraft(null)
      setEdited(false)
      toast('Schedule deleted.', 'warning')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Schedule could not be deleted.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <EmptyState title="Schedules could not load" body={error.message} />

  return <div className="schedules-page">
    <header className="page-heading page-heading--actions"><div><h1>Schedules</h1><p>Automate previews or safe task updates.</p></div><Button icon={Plus} onClick={newSchedule}>New schedule</Button></header>
    <div className="info-band"><Info size={18} /><span>Schedules run while the local server is connected. Missed times are skipped and recorded.</span></div>
    <div className="schedule-layout">
      <section className="schedule-content">
        <h2>Active schedules</h2>
        {data?.items.length ? <div className="table-frame schedules-table"><div className="data-table data-table--schedules data-table__header"><span>Schedule</span><span>Course</span><span>When</span><span>Mode</span><span>Next run</span><span>Last run</span><span>Enabled</span><span /></div>{data.items.map((schedule) => <button key={schedule.id} onClick={() => choose(schedule)} className={`data-table data-table--schedules data-row ${selected?.id === schedule.id && !draft ? 'is-selected' : ''}`}><strong>{schedule.name}</strong><span>{overview?.courses.find((course) => course.id === schedule.course_id)?.settings.name ?? schedule.course_id}</span><span>{weekdayLabel(schedule.weekdays)} / {timeLabel(schedule.local_time)}</span><span>{schedule.mode === 'auto_apply' ? 'Auto-apply safe changes' : 'Preview only'}</span><span>{formatDateTime(schedule.next_run_at)}</span><span>{schedule.last_result ? `${formatDateTime(schedule.last_run_at)} / ${schedule.last_result}` : '-'}</span><span className={`switch ${schedule.enabled ? 'is-on' : ''}`} aria-label={schedule.enabled ? 'Enabled' : 'Disabled'}><i /></span><ArrowRight size={16} /></button>)}</div> : <EmptyState title="No schedules yet" body="Create a preview or safe auto-apply schedule." action={<Button icon={Plus} onClick={newSchedule}>New schedule</Button>} />}

        <section className="scheduled-activity"><h2>Recent scheduled activity</h2>{data?.occurrences.length ? <div className="activity-list">{data.occurrences.map((occurrence) => <div className="activity-row" key={occurrence.id}><span>{formatDateTime(occurrence.scheduled_for)}</span><strong>{data.items.find((schedule) => schedule.id === occurrence.schedule_id)?.name ?? `Schedule #${occurrence.schedule_id}`}</strong><span className={`activity-result activity-result--${occurrence.status}`}>{occurrence.status === 'missed' ? <Clock3 size={16} /> : occurrence.status === 'queued' ? <Check size={16} /> : <AlertTriangle size={16} />}{occurrence.status}</span><span>{occurrence.details}</span>{occurrence.run_id ? <Link to={`/runs/${occurrence.run_id}`} aria-label="Open scheduled run"><ArrowRight size={16} /></Link> : <span />}</div>)}</div> : <p className="muted-copy">Scheduled occurrences appear here after they run or are missed.</p>}</section>
      </section>

      <aside className="schedule-editor inspector-rail">{active ? <><header><h2>{active.id ? 'Edit schedule' : 'New schedule'}</h2>{active.id ? <button className="danger-link" onClick={remove} disabled={busy}>Delete schedule</button> : null}</header><div className="schedule-form"><label className="form-field"><span>Name</span><input value={active.name} onChange={(event) => update('name', event.target.value)} /></label><label className="form-field"><span>Course</span><select value={active.course_id} onChange={(event) => update('course_id', event.target.value)}>{overview?.courses.map((course) => <option key={course.id} value={course.id}>{course.settings.name}</option>)}</select></label><div className="form-field"><span>Days</span><div className="weekday-buttons">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day, index) => <button key={day} className={active.weekdays.includes(index) ? 'is-active' : ''} onClick={() => update('weekdays', active.weekdays.includes(index) ? active.weekdays.filter((value) => value !== index) : [...active.weekdays, index].sort())}>{day}{active.weekdays.includes(index) ? <Check size={12} /> : null}</button>)}</div></div><div className="form-grid form-grid--two"><label className="form-field"><span>Time</span><input type="time" value={active.local_time.slice(0, 5)} onChange={(event) => update('local_time', event.target.value)} /></label><label className="form-field"><span>Timezone</span><input value={active.timezone} onChange={(event) => update('timezone', event.target.value)} /></label></div><fieldset id="schedule-policy" className="radio-section"><legend>Run behavior</legend><label><input type="radio" checked={active.mode === 'preview'} onChange={() => update('mode', 'preview')} /><span><strong>Preview only</strong><small>Generate a preview of changes. Nothing is written.</small></span></label><label><input type="radio" checked={active.mode === 'auto_apply'} onChange={() => update('mode', 'auto_apply')} /><span><strong>Auto-apply safe changes</strong><small>Create and Update actions are applied automatically. Uncertain, missing, and past-due items stay untouched and are flagged for review.</small></span></label></fieldset><section className="safety-rules"><h3>Safety rules</h3><CheckRow>Never delete Google Tasks</CheckRow><CheckRow>Never recreate remote-missing tasks</CheckRow><CheckRow>Past-due writes require manual approval</CheckRow><CheckRow>Apply safe changes when other items need attention</CheckRow></section><label className="form-field"><span>Missed occurrence</span><select disabled><option>Skip and record as missed</option></select><small>Missed runs are recorded and skipped.</small></label><label className="enabled-control"><span className={`switch ${active.enabled ? 'is-on' : ''}`} onClick={() => update('enabled', !active.enabled)}><i /></span><span>Schedule enabled</span></label></div><footer><Button variant="secondary" disabled={!edited} onClick={() => { setDraft(null); setEdited(false) }}>Discard changes</Button><Button icon={Save} disabled={!edited || busy || !active.name || !active.course_id || !active.weekdays.length} onClick={save}>Save schedule</Button></footer></> : <div className="schedule-editor__empty"><CalendarDays size={28} /><h2>Select a schedule</h2><p>Choose a row to inspect its explicit write policy.</p></div>}</aside>
    </div>
  </div>
}

function toDraft(schedule: Schedule): ScheduleDraft {
  return { id: schedule.id, name: schedule.name, course_id: schedule.course_id, weekdays: [...schedule.weekdays], local_time: schedule.local_time, timezone: schedule.timezone, mode: schedule.mode, enabled: schedule.enabled }
}

function weekdayLabel(values: number[]) {
  if (values.join(',') === '0,1,2,3,4') return 'Mon-Fri'
  const labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
  return values.map((value) => labels[value]).join(', ')
}

function timeLabel(value: string) {
  const [hours, minutes] = value.split(':').map(Number)
  return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date(2026, 0, 1, hours, minutes))
}
