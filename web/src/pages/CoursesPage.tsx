import { AlertTriangle, ArrowRight, CheckCircle2, ExternalLink, Plus, Save, Search, TestTube2, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import useSWR, { mutate as globalMutate } from 'swr'
import { useApp } from '../components/AppContext'
import { Button, CheckRow, EmptyState, Modal } from '../components/ui'
import { fetchJson, mutateJson } from '../lib/api'
import type { CourseSettings, CourseView, GeminiModel, GeminiReasoning } from '../types'

const GEMINI_MODELS: GeminiModel[] = [
  'gemini-3.7-flash',
  'gemini-3.6-flash',
  'gemini-3.5-flash',
  'gemini-3.5-flash-lite',
]

const GEMINI_MODEL_LABELS: Record<GeminiModel, string> = {
  'gemini-3.7-flash': '3.7 flash',
  'gemini-3.6-flash': '3.6 flash',
  'gemini-3.5-flash': '3.5 flash',
  'gemini-3.5-flash-lite': '3.5 flash lite',
}

const blankCourse: CourseSettings = {
  enabled: true,
  name: '',
  prefix: '',
  task_list: 'School',
  assessment_task_list: 'Tests',
  ai_instructions: '',
  gemini_model: GEMINI_MODELS[0],
  gemini_fallback_models: GEMINI_MODELS.slice(1),
  gemini_reasoning: 'medium',
  timezone: 'America/New_York',
  meeting_days: ['mon', 'tue', 'wed', 'thu', 'fri'],
  source: {
    type: 'none',
    extraction: {
      mode: 'hybrid',
      thumbnail_size: 'large',
      assignments_default_due: 'next_class',
      same_day_action_kinds: ['bring', 'present', 'submit'],
    },
  },
}

function cloneCourse(value: CourseSettings): CourseSettings {
  const clone = structuredClone(value)
  const order = modelOrder(clone)
  clone.gemini_model = order[0]
  clone.gemini_fallback_models = order.slice(1)
  clone.gemini_reasoning ??= 'medium'
  return clone
}

function modelOrder(settings: CourseSettings): GeminiModel[] {
  const configured = [
    settings.gemini_model,
    ...(settings.gemini_fallback_models ?? []),
  ].filter((model): model is GeminiModel => Boolean(model && GEMINI_MODELS.includes(model)))
  return [...new Set([...configured, ...GEMINI_MODELS])]
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
  const [deleteCourse, setDeleteCourse] = useState<{ id: string; name: string } | null>(null)
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

  async function confirmDelete() {
    if (!deleteCourse) return
    setBusy(true)
    try {
      const updated = await mutateJson<CourseView[]>(`/api/v1/courses/${deleteCourse.id}`, { method: 'DELETE' })
      await mutate(updated, { revalidate: false })
      await Promise.all([
        globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview')),
        globalMutate('/api/v1/schedules'),
      ])
      setSelectedId(updated[0]?.id ?? null)
      setDraft(null)
      setEdited(false)
      setDeleteCourse(null)
      toast('Course deleted. Existing Google Tasks and run history were kept.', 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Course could not be deleted.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <EmptyState title="Courses could not load" body={error.message} />

  return <div className="courses-page">
    <header className="page-heading page-heading--actions"><div><h1>Courses</h1><p>Map each course agenda to Google Tasks.</p></div><Button icon={Plus} onClick={beginAdd}>Add course</Button></header>
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
      </section> : <EmptyState title="Add your first course" body="Connect a browser-captured Google file or a Google Slides API page to a Google Tasks list." action={<Button icon={Plus} onClick={beginAdd}>Add course</Button>} />}
      <aside className="config-inspector"><h2>Configuration</h2>{activeDraft ? <div className="health-list"><CheckRow>Canvas {activeDraft.settings.canvas_course_id ? `course ${activeDraft.settings.canvas_course_id}` : 'needs a fallback'}</CheckRow><CheckRow>{activeDraft.settings.source.type === 'none' ? 'No fallback source' : activeDraft.settings.source.type === 'browser' ? 'Chrome capture fallback' : 'Google Slides fallback'}</CheckRow><CheckRow>{activeDraft.settings.source.extraction.mode} extraction</CheckRow><CheckRow>{activeDraft.settings.task_list || 'Assignments list required'} / {activeDraft.settings.assessment_task_list || 'Tests list required'}</CheckRow><CheckRow>{activeDraft.settings.timezone}</CheckRow></div> : null}<div className="config-file"><h3>Config file</h3><a className="config-file__link" href="/api/v1/courses-config" target="_blank" rel="noreferrer"><span><code>config/courses.yaml</code><small>View sanitized config</small></span><ExternalLink size={15} aria-hidden="true" /></a></div></aside>
    </div>
    {activeDraft && !activeDraft.creating ? <section className="danger-band"><div className="danger-band__actions"><Button variant="danger" icon={AlertTriangle} disabled={busy} onClick={toggleEnabled}>{activeDraft.settings.enabled ? 'Disable course' : 'Enable course'}</Button><Button variant="danger" icon={Trash2} disabled={busy} onClick={() => setDeleteCourse({ id: activeDraft.id, name: activeDraft.settings.name })}>Delete course</Button></div><p>{activeDraft.settings.enabled ? 'Disabling stops future runs. Deleting removes this configuration and its schedules. Neither action deletes existing Google Tasks.' : 'Enable the course to allow previews and schedules again, or delete its local configuration. Existing Google Tasks are never deleted.'}</p></section> : null}
    {deleteCourse ? <Modal title={`Delete ${deleteCourse.name}?`} onClose={() => { if (!busy) setDeleteCourse(null) }} footer={<><Button variant="secondary" disabled={busy} onClick={() => setDeleteCourse(null)}>Cancel</Button><Button variant="danger" icon={Trash2} disabled={busy} onClick={() => void confirmDelete()}>Delete course</Button></>}><div className="confirmation-copy"><AlertTriangle className="tone-danger" size={28} /><p>This permanently removes the course from the local configuration and deletes every schedule assigned to it. Run history and existing Google Tasks are kept.</p></div></Modal> : null}
  </div>
}

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return <label className="form-field"><span>{label}</span>{children}{help ? <small>{help}</small> : null}</label>
}

function GeneralForm({ draft, updateId, update }: { draft: { id: string; settings: CourseSettings; creating: boolean }; updateId: (id: string) => void; update: (recipe: (value: CourseSettings) => void) => void }) {
  return <div className="form-section"><h3>Course details</h3><div className="form-grid form-grid--two"><Field label="Course name"><input value={draft.settings.name} onChange={(event) => update((value) => { value.name = event.target.value })} required /></Field><Field label="Course ID" help={draft.creating ? 'Lowercase identifier; cannot be changed later.' : 'Used in task identities and cannot be changed.'}><input value={draft.id} onChange={(event) => updateId(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ''))} disabled={!draft.creating} required /></Field><Field label="Task title prefix"><input value={draft.settings.prefix} onChange={(event) => update((value) => { value.prefix = event.target.value.toUpperCase() })} /></Field><Field label="Timezone"><input value={draft.settings.timezone} onChange={(event) => update((value) => { value.timezone = event.target.value })} /></Field></div></div>
}

function SourceForm({ settings, update }: { settings: CourseSettings; update: (recipe: (value: CourseSettings) => void) => void }) {
  function switchSource(type: 'none' | 'google_slides' | 'browser') {
    update((value) => {
      const extraction = structuredClone(value.source.extraction)
      const url = value.source.type === 'none' ? '' : value.source.url
      value.source = type === 'none'
        ? { type: 'none', extraction }
        : type === 'browser'
        ? { type: 'browser', url, source_format: 'auto', freshness_seconds: 900, selection: { slide_ids: [], section_ids: [], sheets: [] }, extraction }
        : { type: 'google_slides', url, page_id: '', extraction }
    })
  }
  return <>
    <div className="form-section"><h3>Source</h3>
      <div className="form-grid form-grid--two"><Field label="Canvas course ID" help="When set, Canvas API discovery runs first."><input inputMode="numeric" value={settings.canvas_course_id ?? ''} onChange={(event) => update((value) => { value.canvas_course_id = event.target.value.replace(/\D/g, '') || null })} placeholder="12604" /></Field><Field label="Canvas base URL override" help="Usually inherited from CANVAS_BASE_URL."><input type="url" value={settings.canvas_base_url ?? ''} onChange={(event) => update((value) => { value.canvas_base_url = event.target.value || null })} placeholder="https://school.instructure.com" /></Field></div>
      <Field label="Fallback acquisition" help="Optional. Used only when Canvas cannot provide a verified agenda, or when selected manually."><select value={settings.source.type} onChange={(event) => switchSource(event.target.value as 'none' | 'google_slides' | 'browser')}><option value="none">No fallback · Canvas API only</option><option value="google_slides">Google Slides API · Direct fallback</option><option value="browser">Chrome source connector · Last resort</option></select></Field>
      {settings.source.type === 'none' ? <p className="form-help">Canvas API content is the only agenda source. A failed or unverified week match stops safely for review.</p> : <><Field label={settings.source.type === 'browser' ? 'Google file URL' : 'Presentation URL'} help={settings.source.type === 'browser' ? 'Paste a Google Slides, Docs, or Sheets edit URL.' : 'Paste the Google Slides share or edit link.'}><input type="url" value={settings.source.url} onChange={(event) => update((value) => { if (value.source.type !== 'none') value.source.url = event.target.value })} /></Field>{settings.source.type === 'google_slides' ? <Field label="Slide page ID" help="The stable page ID after slide=id. in the URL."><input value={settings.source.page_id} onChange={(event) => update((value) => { if (value.source.type === 'google_slides') value.source.page_id = event.target.value })} /></Field> : <div className="form-grid form-grid--two"><Field label="Expected format"><select value={settings.source.source_format} onChange={(event) => update((value) => { if (value.source.type === 'browser') value.source.source_format = event.target.value as typeof value.source.source_format })}><option value="auto">Detect from URL</option><option value="google_slides">Google Slides</option><option value="google_docs">Google Docs</option><option value="google_sheets">Google Sheets</option></select></Field><Field label="Capture freshness"><select value={settings.source.freshness_seconds} onChange={(event) => update((value) => { if (value.source.type === 'browser') value.source.freshness_seconds = Number(event.target.value) })}><option value={300}>5 minutes</option><option value={900}>15 minutes</option><option value={1800}>30 minutes</option><option value={3600}>1 hour</option></select></Field></div>}</>}
    </div>
    <div className="form-section"><h3>Extraction</h3><div className="form-grid form-grid--two"><Field label="Gemini mode"><select value={settings.source.extraction.mode} onChange={(event) => update((value) => { value.source.extraction.mode = event.target.value as CourseSettings['source']['extraction']['mode'] })}><option value="auto">Auto</option><option value="hybrid">Hybrid</option><option value="image">Image</option><option value="text">Text</option></select></Field>{settings.source.type === 'google_slides' ? <Field label="Thumbnail size"><select value={settings.source.extraction.thumbnail_size} onChange={(event) => update((value) => { value.source.extraction.thumbnail_size = event.target.value as 'small' | 'medium' | 'large' })}><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option></select></Field> : <Field label="Acquisition"><input value={settings.source.type === 'browser' ? 'Browser extension capture' : 'Canvas API text'} disabled /></Field>}</div><ModelPreferences settings={settings} update={update} /><Field label="Course notes / AI instructions" help="Applied only to this course when Gemini decides which grounded tasks to create. Changes invalidate the extraction cache."><textarea rows={5} value={settings.ai_instructions} onChange={(event) => update((value) => { value.ai_instructions = event.target.value })} placeholder="Example: Do not create homework tasks for reading assignments in this course." /></Field><p className="form-help">Canvas API captures use text directly. A direct Google API source is the next fallback; Chrome capture is used only when configured or explicitly selected.</p></div>
    <div className="form-section"><h3>Destination</h3><div className="form-grid form-grid--three"><Field label="Assignments list"><input value={settings.task_list} onChange={(event) => update((value) => { value.task_list = event.target.value })} /></Field><Field label="Tests & quizzes list"><input value={settings.assessment_task_list} onChange={(event) => update((value) => { value.assessment_task_list = event.target.value })} /></Field><Field label="Task title prefix"><input value={settings.prefix} onChange={(event) => update((value) => { value.prefix = event.target.value.toUpperCase() })} /></Field></div></div>
  </>
}

function ModelPreferences({ settings, update }: { settings: CourseSettings; update: (recipe: (value: CourseSettings) => void) => void }) {
  const order = modelOrder(settings)

  function selectModel(position: number, selected: GeminiModel) {
    update((value) => {
      const next = modelOrder(value)
      const existingPosition = next.indexOf(selected)
      ;[next[position], next[existingPosition]] = [next[existingPosition], next[position]]
      value.gemini_model = next[0]
      value.gemini_fallback_models = next.slice(1)
    })
  }

  return <fieldset className="model-preferences">
    <legend>Model preference</legend>
    <p>Gemini tries the primary model first, then each fallback in order.</p>
    <div className="form-grid form-grid--two">
      <Field label="Reasoning budget" help="Higher reasoning can improve difficult layouts but takes longer and uses more output budget.">
        <select value={settings.gemini_reasoning} onChange={(event) => update((value) => { value.gemini_reasoning = event.target.value as GeminiReasoning })}>
          <option value="low">Low · fastest</option>
          <option value="medium">Medium · recommended</option>
          <option value="high">High · most thorough</option>
        </select>
      </Field>
      {order.map((model, index) => <Field key={`${index}-${model}`} label={index === 0 ? 'Primary model' : `Fallback ${index}`}>
        <select aria-label={index === 0 ? 'Primary model' : `Fallback ${index}`} value={model} onChange={(event) => selectModel(index, event.target.value as GeminiModel)}>
          {GEMINI_MODELS.map((option) => <option key={option} value={option}>{GEMINI_MODEL_LABELS[option]}</option>)}
        </select>
      </Field>)}
    </div>
  </fieldset>
}

function DeadlineForm({ settings, update }: { settings: CourseSettings; update: (recipe: (value: CourseSettings) => void) => void }) {
  const days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
  const actionKinds = ['bring', 'present', 'submit', 'practice', 'complete', 'read', 'study', 'write']
  return <><div className="form-section"><h3>Class calendar</h3><Field label="Meeting days" help="Next-class deadlines advance to the next selected day."><div className="toggle-group">{days.map((day) => <button type="button" className={settings.meeting_days.includes(day) ? 'is-active' : ''} key={day} onClick={() => update((value) => { value.meeting_days = value.meeting_days.includes(day) ? value.meeting_days.filter((item) => item !== day) : [...value.meeting_days, day] })}>{day.slice(0, 1).toUpperCase() + day.slice(1)}</button>)}</div></Field></div><div className="form-section"><h3>Assignment deadlines</h3><Field label="Assignments default due"><select value={settings.source.extraction.assignments_default_due} onChange={(event) => update((value) => { value.source.extraction.assignments_default_due = event.target.value as CourseSettings['source']['extraction']['assignments_default_due'] })}><option value="next_class">Next class</option><option value="same_day">Same agenda day</option><option value="none">No due date</option></select></Field><Field label="Same-day action kinds" help="These commands use the end date of their agenda row."><div className="checkbox-grid">{actionKinds.map((kind) => <label key={kind}><input type="checkbox" checked={settings.source.extraction.same_day_action_kinds.includes(kind)} onChange={() => update((value) => { value.source.extraction.same_day_action_kinds = value.source.extraction.same_day_action_kinds.includes(kind) ? value.source.extraction.same_day_action_kinds.filter((item) => item !== kind) : [...value.source.extraction.same_day_action_kinds, kind] })} />{kind}</label>)}</div></Field></div></>
}
