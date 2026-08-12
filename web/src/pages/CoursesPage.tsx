import { AlertTriangle, ArrowRight, CheckCircle2, Copy, ExternalLink, Plus, Save, Search, TestTube2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import useSWR, { mutate as globalMutate } from 'swr'
import { useApp } from '../components/AppContext'
import { Button, CheckRow, EmptyState } from '../components/ui'
import { fetchJson, mutateJson } from '../lib/api'
import type { CourseSettings, CourseView } from '../types'

const blankCourse: CourseSettings = {
  enabled: true,
  name: '',
  prefix: '',
  task_list: 'School',
  timezone: 'America/New_York',
  meeting_days: ['mon', 'tue', 'wed', 'thu', 'fri'],
  source: {
    type: 'google_slides',
    url: '',
    page_id: '',
    extraction: {
      mode: 'hybrid',
      thumbnail_size: 'large',
      assignments_default_due: 'next_class',
      same_day_action_kinds: ['bring', 'present', 'submit'],
    },
  },
}

function cloneCourse(value: CourseSettings): CourseSettings {
  return structuredClone(value)
}

export default function CoursesPage() {
  const { toast } = useApp()
  const { data: courses, error, mutate } = useSWR<CourseView[]>('/api/v1/courses', fetchJson)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = courses?.find((course) => course.id === selectedId) ?? courses?.[0]
  const [draft, setDraft] = useState<{ id: string; settings: CourseSettings; creating: boolean } | null>(null)
  const activeDraft = draft && (draft.creating || draft.id === selected?.id) ? draft : selected ? { id: selected.id, settings: cloneCourse(selected.settings), creating: false } : null
  const [edited, setEdited] = useState(false)
  const [tab, setTab] = useState<'general' | 'source' | 'deadline'>('source')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const visibleCourses = useMemo(() => (courses ?? []).filter((course) => course.settings.name.toLowerCase().includes(query.toLowerCase())), [courses, query])

  function selectCourse(id: string) {
    setSelectedId(id)
    setDraft(null)
    setEdited(false)
  }

  function beginAdd() {
    setDraft({ id: '', settings: cloneCourse(blankCourse), creating: true })
    setEdited(true)
    setTab('general')
  }

  function updateSettings(recipe: (value: CourseSettings) => void) {
    if (!activeDraft) return
    const next = cloneCourse(activeDraft.settings)
    recipe(next)
    setDraft({ ...activeDraft, settings: next })
    setEdited(true)
  }

  function updateId(id: string) {
    if (!activeDraft) return
    setDraft({ ...activeDraft, id })
    setEdited(true)
  }

  async function save() {
    if (!activeDraft) return
    setBusy(true)
    try {
      const url = activeDraft.creating ? '/api/v1/courses' : `/api/v1/courses/${activeDraft.id}`
      const updated = await mutateJson<CourseView[]>(url, { method: activeDraft.creating ? 'POST' : 'PUT', body: activeDraft })
      await mutate(updated, { revalidate: false })
      await globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
      setSelectedId(activeDraft.id)
      setDraft(null)
      setEdited(false)
      toast(activeDraft.creating ? 'Course added.' : 'Course changes saved.', 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Course could not be saved.', 'error')
    } finally { setBusy(false) }
  }

  async function testSource() {
    if (!activeDraft || activeDraft.creating || edited) {
      toast('Save this course before testing its live source.', 'warning')
      return
    }
    setBusy(true)
    try {
      const result = await mutateJson<{ checks: { state: string; summary: string }[] }>(`/api/v1/courses/${activeDraft.id}/test`)
      const failure = result.checks.find((check) => check.state === 'error')
      toast(failure?.summary ?? 'Source and destination checks passed.', failure ? 'error' : 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Source test failed.', 'error')
    } finally { setBusy(false) }
  }

  async function toggleEnabled() {
    if (!activeDraft || activeDraft.creating) return
    if (activeDraft.settings.enabled && !window.confirm('Disable this course? Future runs stop, but existing Google Tasks are never deleted.')) return
    setBusy(true)
    try {
      const verb = activeDraft.settings.enabled ? 'disable' : 'enable'
      const updated = await mutateJson<CourseView[]>(`/api/v1/courses/${activeDraft.id}/${verb}`)
      await mutate(updated, { revalidate: false })
      setDraft(null)
      toast(`Course ${verb}d.`, activeDraft.settings.enabled ? 'warning' : 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Course status could not change.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <EmptyState title="Courses could not load" body={error.message} />

  return <div className="courses-page">
    <header className="page-heading page-heading--actions"><div><h1>Courses</h1><p>Manage how each agenda becomes Google Tasks.</p></div><Button icon={Plus} onClick={beginAdd}>Add course</Button></header>
    <div className="course-editor-layout panel">
      <aside className="course-list"><label className="search-control"><Search size={16} /><input placeholder="Search courses" value={query} onChange={(event) => setQuery(event.target.value)} /></label>{visibleCourses.map((course) => <button key={course.id} className={selected?.id === course.id && !activeDraft?.creating ? 'course-list__item is-active' : 'course-list__item'} onClick={() => selectCourse(course.id)}><span>{course.settings.name}</span><small><i className={`dot dot--${course.readiness}`} />{course.readiness_message}</small><ArrowRight size={16} /></button>)}<div className="course-list__footer">Course IDs stay fixed after creation</div></aside>
      {activeDraft ? <section className="course-form">
        <header className="editor-heading"><div><h2>{activeDraft.creating ? 'Add course' : activeDraft.settings.name}</h2><span className={edited ? 'tone-warning' : 'tone-success'}>{edited ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}{edited ? 'Unsaved changes' : 'No unsaved changes'}</span></div><div><Button variant="secondary" icon={TestTube2} disabled={busy || activeDraft.creating} onClick={testSource}>Test source</Button><Button icon={Save} disabled={busy || !edited} onClick={save}>Save changes</Button></div></header>
        <div className="tab-bar"><button className={tab === 'general' ? 'is-active' : ''} onClick={() => setTab('general')}>General</button><button className={tab === 'source' ? 'is-active' : ''} onClick={() => setTab('source')}>Source &amp; extraction</button><button className={tab === 'deadline' ? 'is-active' : ''} onClick={() => setTab('deadline')}>Deadline policy</button></div>
        <div className="form-scroll">
          {tab === 'general' ? <GeneralForm draft={activeDraft} updateId={updateId} update={updateSettings} /> : null}
          {tab === 'source' ? <SourceForm settings={activeDraft.settings} update={updateSettings} /> : null}
          {tab === 'deadline' ? <DeadlineForm settings={activeDraft.settings} update={updateSettings} /> : null}
        </div>
        <footer className="editor-actions"><Button variant="secondary" onClick={() => { setDraft(null); setEdited(false) }} disabled={!edited}>Discard changes</Button><Button icon={Save} disabled={busy || !edited} onClick={save}>Save changes</Button></footer>
      </section> : <EmptyState title="Add your first course" body="Connect a Google Slides agenda to a Google Tasks list." action={<Button icon={Plus} onClick={beginAdd}>Add course</Button>} />}
      <aside className="config-inspector"><h2>Configuration checks</h2>{activeDraft ? <div className="health-list"><CheckRow>Presentation URL · {activeDraft.settings.source.url ? 'Configured' : 'Required'}</CheckRow><CheckRow>Slide page · {activeDraft.settings.source.page_id ? 'Configured' : 'Required'}</CheckRow><CheckRow>Extraction · {activeDraft.settings.source.extraction.mode} · {activeDraft.settings.source.extraction.thumbnail_size}</CheckRow><CheckRow>Google Tasks list · {activeDraft.settings.task_list || 'Required'}</CheckRow><CheckRow>Timezone · {activeDraft.settings.timezone}</CheckRow></div> : null}<div className="config-file"><h3>Config file</h3><div><span>config/courses.yaml</span><Copy size={16} /></div><a className="inline-link" href="/api/v1/courses-config" target="_blank" rel="noreferrer">View sanitized config <ExternalLink size={15} /></a><small>Secrets are never stored in this file.</small></div></aside>
    </div>
    {activeDraft && !activeDraft.creating ? <section className="danger-band"><Button variant="danger" icon={AlertTriangle} disabled={busy} onClick={toggleEnabled}>{activeDraft.settings.enabled ? 'Disable course' : 'Enable course'}</Button><p>{activeDraft.settings.enabled ? 'Disabling stops future runs for this course. It never deletes Google Tasks.' : 'Enable the course to allow previews and schedules again.'}</p></section> : null}
  </div>
}

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return <label className="form-field"><span>{label}</span>{children}{help ? <small>{help}</small> : null}</label>
}

function GeneralForm({ draft, updateId, update }: { draft: { id: string; settings: CourseSettings; creating: boolean }; updateId: (id: string) => void; update: (recipe: (value: CourseSettings) => void) => void }) {
  return <div className="form-section"><h3>Course details</h3><div className="form-grid form-grid--two"><Field label="Course name"><input value={draft.settings.name} onChange={(event) => update((value) => { value.name = event.target.value })} required /></Field><Field label="Course ID" help={draft.creating ? 'Lowercase identifier; cannot be changed later.' : 'Used in task identities and cannot be changed.'}><input value={draft.id} onChange={(event) => updateId(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ''))} disabled={!draft.creating} required /></Field><Field label="Task title prefix"><input value={draft.settings.prefix} onChange={(event) => update((value) => { value.prefix = event.target.value.toUpperCase() })} /></Field><Field label="Timezone"><input value={draft.settings.timezone} onChange={(event) => update((value) => { value.timezone = event.target.value })} /></Field></div></div>
}

function SourceForm({ settings, update }: { settings: CourseSettings; update: (recipe: (value: CourseSettings) => void) => void }) {
  return <><div className="form-section"><h3>Source</h3><Field label="Source type" help="v1 supports Google Slides only."><input value="Google Slides" disabled /></Field><Field label="Presentation URL" help="Paste the Google Slides share or edit link."><input type="url" value={settings.source.url} onChange={(event) => update((value) => { value.source.url = event.target.value })} /></Field><Field label="Slide page ID" help="The stable page ID after slide=id. in the URL."><input value={settings.source.page_id} onChange={(event) => update((value) => { value.source.page_id = event.target.value })} /></Field></div><div className="form-section"><h3>Extraction</h3><div className="form-grid form-grid--three"><Field label="Mode"><select value={settings.source.extraction.mode} onChange={(event) => update((value) => { value.source.extraction.mode = event.target.value as CourseSettings['source']['extraction']['mode'] })}><option value="auto">Auto</option><option value="hybrid">Hybrid</option><option value="image">Image</option><option value="text">Text</option></select></Field><Field label="Thumbnail size"><select value={settings.source.extraction.thumbnail_size} onChange={(event) => update((value) => { value.source.extraction.thumbnail_size = event.target.value as 'small' | 'medium' | 'large' })}><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option></select></Field><Field label="Model (inherited)"><input value="gemini-3.6-flash" disabled /></Field></div><p className="form-help"><em>Hybrid</em> uses slide thumbnail images and exact text together for higher accuracy.</p></div><div className="form-section"><h3>Destination</h3><div className="form-grid form-grid--two"><Field label="Google Tasks list"><input value={settings.task_list} onChange={(event) => update((value) => { value.task_list = event.target.value })} /></Field><Field label="Task title prefix"><input value={settings.prefix} onChange={(event) => update((value) => { value.prefix = event.target.value.toUpperCase() })} /></Field></div></div></>
}

function DeadlineForm({ settings, update }: { settings: CourseSettings; update: (recipe: (value: CourseSettings) => void) => void }) {
  const days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
  const actionKinds = ['bring', 'present', 'submit', 'practice', 'complete', 'read', 'study', 'write']
  return <><div className="form-section"><h3>Class calendar</h3><Field label="Meeting days" help="Next-class deadlines advance to the next selected day."><div className="toggle-group">{days.map((day) => <button type="button" className={settings.meeting_days.includes(day) ? 'is-active' : ''} key={day} onClick={() => update((value) => { value.meeting_days = value.meeting_days.includes(day) ? value.meeting_days.filter((item) => item !== day) : [...value.meeting_days, day] })}>{day.slice(0, 1).toUpperCase() + day.slice(1)}</button>)}</div></Field></div><div className="form-section"><h3>Assignment deadlines</h3><Field label="Assignments default due"><select value={settings.source.extraction.assignments_default_due} onChange={(event) => update((value) => { value.source.extraction.assignments_default_due = event.target.value as CourseSettings['source']['extraction']['assignments_default_due'] })}><option value="next_class">Next class</option><option value="same_day">Same agenda day</option><option value="none">No due date</option></select></Field><Field label="Same-day action kinds" help="These commands use the end date of their agenda row."><div className="checkbox-grid">{actionKinds.map((kind) => <label key={kind}><input type="checkbox" checked={settings.source.extraction.same_day_action_kinds.includes(kind)} onChange={() => update((value) => { value.source.extraction.same_day_action_kinds = value.source.extraction.same_day_action_kinds.includes(kind) ? value.source.extraction.same_day_action_kinds.filter((item) => item !== kind) : [...value.source.extraction.same_day_action_kinds, kind] })} />{kind}</label>)}</div></Field></div></>
}
