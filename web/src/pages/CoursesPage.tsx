import {
  ArrowSquareOutIcon,
  BooksIcon,
  CheckCircleIcon,
  FlaskIcon,
  MagnifyingGlassIcon,
  PauseCircleIcon,
  PlayCircleIcon,
  PlusIcon,
  TrashIcon,
  WarningCircleIcon,
  XCircleIcon,
} from '@phosphor-icons/react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import useSWR, { mutate as globalMutate } from 'swr'
import { useApp } from '../components/AppContext'
import { AgendaOverrideForm } from '../components/AgendaOverrideForm'
import {
  Button,
  Disclosure,
  EmptyState,
  Field,
  FieldGroup,
  Modal,
  Notice,
  PageHeader,
  SkeletonRows,
  StatusIcon,
} from '../components/ui'
import { fetchJson, mutateJson, revalidateOverview } from '../lib/api'
import { agendaOverrideExpiry, agendaOverrideValidationMessage } from '../lib/agenda-override'
import type { CourseSettings, CourseView, ExtractionAgentView, GeminiModel, GeminiReasoning } from '../types'

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

const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const ACTION_KINDS = ['bring', 'present', 'submit', 'practice', 'complete', 'read', 'study', 'write']

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

interface Draft { id: string; settings: CourseSettings; creating: boolean }
type Update = (recipe: (value: CourseSettings) => void) => void

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

function slugify(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40)
}

export default function CoursesPage() {
  const { toast } = useApp()
  const [searchParams] = useSearchParams()
  const { data: courses, error, mutate } = useSWR<CourseView[]>('/api/v1/courses', fetchJson)
  const [selectedId, setSelectedId] = useState<string | null>(searchParams.get('course'))
  const selected = courses?.find((course) => course.id === selectedId) ?? courses?.[0]
  const [draft, setDraft] = useState<Draft | null>(() => searchParams.get('new') ? { id: '', settings: cloneCourse(blankCourse), creating: true } : null)
  const activeDraft = draft && (draft.creating || draft.id === selected?.id) ? draft : selected ? { id: selected.id, settings: cloneCourse(selected.settings), creating: false } : null
  const [edited, setEdited] = useState(Boolean(draft?.creating))
  const [idTouched, setIdTouched] = useState(false)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<'delete' | 'disable' | null>(null)
  const visibleCourses = useMemo(() => (courses ?? []).filter((course) => course.settings.name.toLowerCase().includes(query.toLowerCase())), [courses, query])

  function selectCourse(id: string) {
    setSelectedId(id)
    setDraft(null)
    setEdited(false)
  }

  function beginAdd() {
    setDraft({ id: '', settings: cloneCourse(blankCourse), creating: true })
    setIdTouched(false)
    setEdited(true)
  }

  function discard() {
    setDraft(null)
    setEdited(false)
  }

  const updateSettings: Update = (recipe) => {
    if (!activeDraft) return
    const next = cloneCourse(activeDraft.settings)
    recipe(next)
    const id = activeDraft.creating && !idTouched ? slugify(next.name) : activeDraft.id
    setDraft({ ...activeDraft, id, settings: next })
    setEdited(true)
  }

  function updateId(id: string) {
    if (!activeDraft) return
    setDraft({ ...activeDraft, id })
    setIdTouched(true)
    setEdited(true)
  }

  async function save() {
    if (!activeDraft) return
    const overrideError = agendaOverrideValidationMessage(activeDraft.settings)
    if (overrideError) {
      toast(overrideError, 'warning')
      return
    }
    if (activeDraft.creating && (!activeDraft.settings.name.trim() || !activeDraft.id)) {
      toast('Give the course a name and an ID before saving.', 'warning')
      return
    }
    setBusy(true)
    try {
      const url = activeDraft.creating ? '/api/v1/courses' : `/api/v1/courses/${activeDraft.id}`
      const updated = await mutateJson<CourseView[]>(url, { method: activeDraft.creating ? 'POST' : 'PUT', body: activeDraft })
      await mutate(updated, { revalidate: false })
      await revalidateOverview()
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

  async function setEnabled(enabled: boolean) {
    if (!activeDraft || activeDraft.creating) return
    setBusy(true)
    try {
      const verb = enabled ? 'enable' : 'disable'
      const updated = await mutateJson<CourseView[]>(`/api/v1/courses/${activeDraft.id}/${verb}`)
      await mutate(updated, { revalidate: false })
      await revalidateOverview()
      setDraft(null)
      setEdited(false)
      setConfirm(null)
      toast(`Course ${verb}d.`, enabled ? 'success' : 'warning')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Course status could not change.', 'error')
    } finally { setBusy(false) }
  }

  async function confirmDelete() {
    if (!activeDraft || activeDraft.creating) return
    setBusy(true)
    try {
      const updated = await mutateJson<CourseView[]>(`/api/v1/courses/${activeDraft.id}`, { method: 'DELETE' })
      await mutate(updated, { revalidate: false })
      await Promise.all([revalidateOverview(), globalMutate('/api/v1/schedules')])
      setSelectedId(updated[0]?.id ?? null)
      setDraft(null)
      setEdited(false)
      setConfirm(null)
      toast('Course deleted. Existing Google Tasks and run history were kept.', 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Course could not be deleted.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <div className="surface"><EmptyState icon={XCircleIcon} title="Courses could not load" body={error.message} /></div>

  const settings = activeDraft?.settings
  const expiry = settings ? agendaOverrideExpiry(settings) : null

  return <div className="page--courses">
    <PageHeader
      title="Courses"
      description="Where each course's agenda comes from, and where its tasks land in Google Tasks."
      actions={<a className="text-link" href="/api/v1/courses-config" target="_blank" rel="noreferrer">View config file<ArrowSquareOutIcon size={14} aria-hidden /></a>}
    />
    {!courses ? <SkeletonRows rows={3} label="Loading courses" /> : <div className="courses-layout">
      <nav className="course-list" aria-label="Course list">
        {courses.length > 6 ? <label className="search course-list__search">
          <MagnifyingGlassIcon size={16} aria-hidden />
          <input className="control control--sm" aria-label="Search courses" placeholder="Search courses" value={query} onChange={(event) => setQuery(event.target.value)} />
        </label> : null}
        {visibleCourses.map((course) => {
          const active = selected?.id === course.id && !activeDraft?.creating
          return <button
            type="button"
            key={course.id}
            className={`course-list__item${active ? ' is-active' : ''}`}
            aria-current={active ? 'true' : undefined}
            onClick={() => selectCourse(course.id)}
          >
            <StatusIcon state={course.settings.enabled ? course.readiness : 'missing'} size={16} />
            <span className="course-list__text">
              <strong className="truncate">{course.settings.name}</strong>
              <small className="truncate">{course.settings.enabled ? course.readiness_message : 'Disabled'}</small>
            </span>
          </button>
        })}
        {activeDraft?.creating ? <div className="course-list__item is-active" aria-current="true">
          <PlusIcon size={16} aria-hidden />
          <span className="course-list__text"><strong className="truncate">{activeDraft.settings.name || 'New course'}</strong><small>Not saved yet</small></span>
        </div> : null}
        <Button variant="ghost" icon={PlusIcon} className="course-list__add" disabled={Boolean(activeDraft?.creating)} onClick={beginAdd}>Add course</Button>
      </nav>

      {activeDraft && settings ? <section className="editor" aria-label="Course editor">
        <header className="editor-head">
          <div className="editor-head__text">
            <h2>{activeDraft.creating ? 'New course' : settings.name || 'Untitled course'}</h2>
            <div className="editor-head__meta">
              {activeDraft.creating
                ? <span>Fill in the basics and save. You can test the source afterwards.</span>
                : <>{settings.enabled ? <span className="badge badge--success">Enabled</span> : <span className="badge">Disabled</span>}<span className="mono subtle">{activeDraft.id}</span></>}
            </div>
          </div>
          {!activeDraft.creating ? <div className="editor-head__actions">
            <Button variant="secondary" size="sm" icon={FlaskIcon} disabled={busy} onClick={() => void testSource()}>Test source</Button>
          </div> : null}
        </header>

        {expiry && !expiry.expired ? <Notice tone="info" title="A temporary agenda override is active">It applies through {expiry.expiresOn}. You'll find it under Advanced.</Notice> : null}

        <div className="form-section">
          <div className="form-section__intro"><h3>Basics</h3><p>How the course is named in this app and in task titles.</p></div>
          <div className="form-section__fields form-grid">
            <Field label="Course name">
              <input className="control" value={settings.name} onChange={(event) => updateSettings((value) => { value.name = event.target.value })} placeholder="AP Psychology" required />
            </Field>
            <Field label="Course ID" help={activeDraft.creating ? "Lowercase letters, numbers, and dashes. It can't change later." : "Used in task identities, so it can't change."}>
              <input className="control control--mono" value={activeDraft.id} onChange={(event) => updateId(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ''))} disabled={!activeDraft.creating} required />
            </Field>
            <Field label="Task title prefix" help="Shown in brackets at the start of every task title.">
              <input className="control" value={settings.prefix} onChange={(event) => updateSettings((value) => { value.prefix = event.target.value.toUpperCase() })} placeholder="PSYCH" />
            </Field>
            <Field label="Timezone" help="An IANA name, such as America/New_York.">
              <input className="control" value={settings.timezone} onChange={(event) => updateSettings((value) => { value.timezone = event.target.value })} />
            </Field>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section__intro"><h3>Agenda source</h3><p>Canvas is always read first. A fallback is used only when Canvas can't provide a verified agenda.</p></div>
          <div className="form-section__fields">
            <Field label="Canvas course ID" help="The number after /courses/ in the Canvas address.">
              <input className="control" inputMode="numeric" value={settings.canvas_course_id ?? ''} onChange={(event) => updateSettings((value) => { value.canvas_course_id = event.target.value.replace(/\D/g, '') || null })} placeholder="12604" />
            </Field>
            <SourceFields settings={settings} update={updateSettings} />
          </div>
        </div>

        <div className="form-section">
          <div className="form-section__intro"><h3>Google Tasks</h3><p>Assignments and assessments go to separate lists.</p></div>
          <div className="form-section__fields form-grid">
            <Field label="Assignments list"><input className="control" value={settings.task_list} onChange={(event) => updateSettings((value) => { value.task_list = event.target.value })} /></Field>
            <Field label="Tests & quizzes list"><input className="control" value={settings.assessment_task_list} onChange={(event) => updateSettings((value) => { value.assessment_task_list = event.target.value })} /></Field>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section__intro"><h3>Due dates</h3><p>Used when the agenda doesn't state a date. Stated dates always win.</p></div>
          <div className="form-section__fields">
            <FieldGroup label="Class meets on" help="Work due “next class” lands on the next of these days.">
              <div className="chips">
                {WEEKDAYS.map((day) => <button
                  type="button"
                  className="chip"
                  aria-pressed={settings.meeting_days.includes(day)}
                  key={day}
                  onClick={() => updateSettings((value) => { value.meeting_days = value.meeting_days.includes(day) ? value.meeting_days.filter((item) => item !== day) : [...value.meeting_days, day] })}
                >{day[0].toUpperCase() + day.slice(1)}</button>)}
              </div>
            </FieldGroup>
            <Field label="Assignments are due">
              <select className="control" value={settings.source.extraction.assignments_default_due} onChange={(event) => updateSettings((value) => { value.source.extraction.assignments_default_due = event.target.value as CourseSettings['source']['extraction']['assignments_default_due'] })}>
                <option value="next_class">At the next class</option>
                <option value="same_day">On the same agenda day</option>
                <option value="none">No due date</option>
              </select>
            </Field>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section__intro"><h3>AI instructions</h3><p>Optional guidance for this course only.</p></div>
          <div className="form-section__fields">
            <Field label="AI instructions" help="Applied when deciding which grounded tasks to create. Changing this re-reads the agenda once.">
              <textarea className="control" rows={4} value={settings.ai_instructions} onChange={(event) => updateSettings((value) => { value.ai_instructions = event.target.value })} placeholder="Example: Don't create tasks for in-class reading." />
            </Field>
          </div>
        </div>

        <Disclosure key={activeDraft.creating ? 'new' : activeDraft.id} title="Advanced" hint="Extraction, Gemini models, and agenda overrides" defaultOpen={Boolean(settings.canvas_agenda_override)}>
          <AdvancedFields settings={settings} update={updateSettings} />
        </Disclosure>

        {!activeDraft.creating ? <div className="form-section">
          <div className="form-section__intro"><h3>Course status</h3><p>Neither action deletes existing Google Tasks.</p></div>
          <div className="form-section__fields">
            <div className="surface danger-zone">
              <div className="setting">
                <div className="setting__text">
                  <span className="setting__title">{settings.enabled ? 'Disable course' : 'Enable course'}</span>
                  <span className="setting__desc">{settings.enabled ? 'Stops syncs and schedules for this course until you enable it again.' : 'Allows syncs, previews, and schedules again.'}</span>
                </div>
                <div className="setting__control">
                  {settings.enabled
                    ? <Button variant="secondary" icon={PauseCircleIcon} disabled={busy} onClick={() => setConfirm('disable')}>Disable course</Button>
                    : <Button variant="secondary" icon={PlayCircleIcon} disabled={busy} onClick={() => void setEnabled(true)}>Enable course</Button>}
                </div>
              </div>
              <div className="setting">
                <div className="setting__text">
                  <span className="setting__title">Delete course</span>
                  <span className="setting__desc">Removes it from the configuration, along with its schedules. Run history is kept.</span>
                </div>
                <div className="setting__control"><Button variant="danger" icon={TrashIcon} disabled={busy} onClick={() => setConfirm('delete')}>Delete course</Button></div>
              </div>
            </div>
          </div>
        </div> : null}

        <div className={`save-bar${edited ? ' is-dirty' : ''}`}>
          <span className="save-bar__status">
            {edited ? <WarningCircleIcon className="tone-warning" size={17} weight="fill" aria-hidden /> : <CheckCircleIcon className="tone-success" size={17} weight="fill" aria-hidden />}
            {activeDraft.creating ? 'New course, not saved yet' : edited ? 'Unsaved changes' : 'All changes saved'}
          </span>
          <div className="save-bar__actions">
            <Button variant="ghost" onClick={discard} disabled={!edited || busy}>{activeDraft.creating ? 'Cancel' : 'Discard'}</Button>
            <Button loading={busy} disabled={!edited} onClick={() => void save()}>{activeDraft.creating ? 'Create course' : 'Save changes'}</Button>
          </div>
        </div>
      </section> : <div className="surface"><EmptyState icon={BooksIcon} title="Add your first course" body="Connect a Canvas course and choose the Google Tasks lists its work should go to." action={<Button icon={PlusIcon} onClick={beginAdd}>Add course</Button>} /></div>}
    </div>}

    {confirm === 'delete' && activeDraft ? <Modal
      title={`Delete ${activeDraft.settings.name}?`}
      onClose={() => { if (!busy) setConfirm(null) }}
      footer={<><Button variant="secondary" disabled={busy} onClick={() => setConfirm(null)}>Cancel</Button><Button variant="danger-solid" icon={TrashIcon} loading={busy} onClick={() => void confirmDelete()}>Delete course</Button></>}
    >
      <p>This permanently removes the course from the local configuration and deletes every schedule assigned to it. Run history and existing Google Tasks are kept.</p>
    </Modal> : null}
    {confirm === 'disable' && activeDraft ? <Modal
      title={`Disable ${activeDraft.settings.name}?`}
      onClose={() => { if (!busy) setConfirm(null) }}
      footer={<><Button variant="secondary" disabled={busy} onClick={() => setConfirm(null)}>Cancel</Button><Button icon={PauseCircleIcon} loading={busy} onClick={() => void setEnabled(false)}>Disable course</Button></>}
    >
      <p>Future syncs and schedules for this course stop. Existing Google Tasks are never deleted.</p>
    </Modal> : null}
  </div>
}

const SOURCE_CHOICES = [
  { value: 'none', title: 'None', description: 'Canvas only.' },
  { value: 'google_slides', title: 'Google Slides', description: 'Read one slide with the Slides API.' },
  { value: 'browser', title: 'Chrome capture', description: 'Capture a Google file with the extension.' },
] as const

function SourceFields({ settings, update }: { settings: CourseSettings; update: Update }) {
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
    <FieldGroup label="Fallback source">
      <div className="choice-grid" role="radiogroup" aria-label="Fallback source">
        {SOURCE_CHOICES.map((choice) => <label className={`choice${settings.source.type === choice.value ? ' is-selected' : ''}`} key={choice.value}>
          <input type="radio" name="fallback-source" value={choice.value} checked={settings.source.type === choice.value} onChange={() => switchSource(choice.value)} />
          <span className="choice__title">{choice.title}</span>
          <span className="choice__desc">{choice.description}</span>
        </label>)}
      </div>
    </FieldGroup>
    {settings.source.type === 'none'
      ? <p className="field__help">Canvas API content is the only agenda source. If a week can't be verified, the run stops safely for review.</p>
      : <div className="form-panel">
        <Field label={settings.source.type === 'browser' ? 'Google file URL' : 'Presentation URL'} help={settings.source.type === 'browser' ? 'A Google Slides, Docs, or Sheets edit link.' : 'The Google Slides share or edit link.'}>
          <input className="control" type="url" value={settings.source.url} onChange={(event) => update((value) => { if (value.source.type !== 'none') value.source.url = event.target.value })} />
        </Field>
        {settings.source.type === 'google_slides'
          ? <Field label="Slide page ID" help="The ID after slide=id. in the slide's address.">
            <input className="control control--mono" value={settings.source.page_id} onChange={(event) => update((value) => { if (value.source.type === 'google_slides') value.source.page_id = event.target.value })} />
          </Field>
          : <div className="form-grid">
            <Field label="File type">
              <select className="control" value={settings.source.source_format} onChange={(event) => update((value) => { if (value.source.type === 'browser') value.source.source_format = event.target.value as typeof value.source.source_format })}>
                <option value="auto">Detect from the URL</option><option value="google_slides">Google Slides</option><option value="google_docs">Google Docs</option><option value="google_sheets">Google Sheets</option>
              </select>
            </Field>
            <Field label="Reuse a capture for">
              <select className="control" value={settings.source.freshness_seconds} onChange={(event) => update((value) => { if (value.source.type === 'browser') value.source.freshness_seconds = Number(event.target.value) })}>
                <option value={300}>5 minutes</option><option value={900}>15 minutes</option><option value={1800}>30 minutes</option><option value={3600}>1 hour</option>
              </select>
            </Field>
          </div>}
      </div>}
  </>
}

function AdvancedFields({ settings, update }: { settings: CourseSettings; update: Update }) {
  return <div className="form-stack">
    <div className="form-panel">
      <h3>Extraction</h3>
      <div className="form-grid">
        <Field label="Extraction mode" help="Hybrid reads both text and images.">
          <select className="control" value={settings.source.extraction.mode} onChange={(event) => update((value) => { value.source.extraction.mode = event.target.value as CourseSettings['source']['extraction']['mode'] })}>
            <option value="hybrid">Hybrid</option><option value="auto">Auto</option><option value="image">Image</option><option value="text">Text</option>
          </select>
        </Field>
        {settings.source.type === 'google_slides' ? <Field label="Thumbnail size">
          <select className="control" value={settings.source.extraction.thumbnail_size} onChange={(event) => update((value) => { value.source.extraction.thumbnail_size = event.target.value as 'small' | 'medium' | 'large' })}>
            <option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option>
          </select>
        </Field> : null}
      </div>
      <FieldGroup label="Due the same day" help="Agenda rows with these verbs are due on the row's own date instead of the next class.">
        <div className="chips">
          {ACTION_KINDS.map((kind) => <button
            type="button"
            className="chip"
            key={kind}
            aria-pressed={settings.source.extraction.same_day_action_kinds.includes(kind)}
            onClick={() => update((value) => {
              const kinds = value.source.extraction.same_day_action_kinds
              value.source.extraction.same_day_action_kinds = kinds.includes(kind) ? kinds.filter((item) => item !== kind) : [...kinds, kind]
            })}
          >{kind[0].toUpperCase() + kind.slice(1)}</button>)}
        </div>
      </FieldGroup>
    </div>
    <ModelPreferences settings={settings} update={update} />
    <div className="form-panel">
      <h3>Canvas address</h3>
      <Field label="Canvas base URL override" help="Leave empty to use CANVAS_BASE_URL from the server.">
        <input className="control" type="url" value={settings.canvas_base_url ?? ''} onChange={(event) => update((value) => { value.canvas_base_url = event.target.value || null })} placeholder="https://school.instructure.com" />
      </Field>
    </div>
    <AgendaOverrideForm settings={settings} update={update} />
  </div>
}

function ModelPreferences({ settings, update }: { settings: CourseSettings; update: Update }) {
  const order = modelOrder(settings)
  const { data: agent } = useSWR<ExtractionAgentView>('/api/v1/settings/extraction-agent', fetchJson)
  const overridden = Boolean(agent?.settings && (agent.settings.provider !== 'gemini' || agent.settings.model !== null))

  function selectModel(position: number, selected: GeminiModel) {
    update((value) => {
      const next = modelOrder(value)
      const existingPosition = next.indexOf(selected)
      ;[next[position], next[existingPosition]] = [next[existingPosition], next[position]]
      value.gemini_model = next[0]
      value.gemini_fallback_models = next.slice(1)
    })
  }

  return <fieldset className="form-panel">
    <div className="form-panel__head"><h3>Gemini models</h3>{overridden ? <span className="badge">Not in use</span> : null}</div>
    <p className="field__help">{overridden
      ? `Every course currently uses ${agent?.label}, chosen in Settings. These apply only if you switch back to Gemini with per-course models.`
      : 'Gemini tries the primary model first, then each fallback in order.'}</p>
    <div className="form-grid">
      <Field label="Reasoning" help="Higher reasoning helps with hard layouts but takes longer.">
        <select className="control" value={settings.gemini_reasoning} onChange={(event) => update((value) => { value.gemini_reasoning = event.target.value as GeminiReasoning })}>
          <option value="low">Low, fastest</option>
          <option value="medium">Medium (recommended)</option>
          <option value="high">High, most thorough</option>
        </select>
      </Field>
      {order.map((model, index) => <Field key={`${index}-${model}`} label={index === 0 ? 'Primary model' : `Fallback ${index}`}>
        <select className="control" aria-label={index === 0 ? 'Primary model' : `Fallback ${index}`} value={model} onChange={(event) => selectModel(index, event.target.value as GeminiModel)}>
          {GEMINI_MODELS.map((option) => <option key={option} value={option}>{GEMINI_MODEL_LABELS[option]}</option>)}
        </select>
      </Field>)}
    </div>
  </fieldset>
}
