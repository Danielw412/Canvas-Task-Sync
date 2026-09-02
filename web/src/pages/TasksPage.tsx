import { CalendarDays, CheckCircle2, Edit3, Plus, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import useSWR, { mutate } from 'swr'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, Modal } from '../components/ui'
import { fetchJson, mutateJson } from '../lib/api'
import type { CourseView, ManualTaskInput, TrackedTask } from '../types'

type TaskFilter = 'open' | 'completed' | 'all'

const ACTIONS = ['complete', 'practice', 'bring', 'present', 'submit', 'read', 'study', 'write', 'other']

export default function TasksPage() {
  const { selectedCourseId, toast } = useApp()
  const { data: tasks, error, isLoading } = useSWR<TrackedTask[]>('/api/v1/tasks', fetchJson)
  const { data: courses } = useSWR<CourseView[]>('/api/v1/courses', fetchJson)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<TaskFilter>('open')
  const [editing, setEditing] = useState<TrackedTask | 'new' | null>(null)
  const filtered = useMemo(() => (tasks ?? []).filter((task) => {
    if (selectedCourseId && task.course.id !== selectedCourseId) return false
    if (filter === 'open' && task.completed !== false) return false
    if (filter === 'completed' && task.completed !== true) return false
    const terms = `${task.display_title} ${task.details} ${task.course.name}`.toLocaleLowerCase()
    return !query || terms.includes(query.toLocaleLowerCase())
  }), [filter, query, selectedCourseId, tasks])

  return <div className="standard-page tasks-page">
    <header className="page-heading page-heading--actions">
      <div><h1>Tasks</h1><p>Create and edit tracked work. Changes are written directly to Google Tasks.</p></div>
      <Button icon={Plus} disabled={!courses?.length} onClick={() => setEditing('new')}>New task</Button>
    </header>
    <section className="task-toolbar panel">
      <label className="task-search"><Search size={17} /><input aria-label="Search tasks" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search tasks" /></label>
      <div className="task-filter" aria-label="Task status filter">
        {(['open', 'completed', 'all'] as TaskFilter[]).map((value) => <button key={value} className={filter === value ? 'is-active' : ''} onClick={() => setFilter(value)}>{value === 'open' ? 'Open' : value === 'completed' ? 'Completed' : 'All'}</button>)}
      </div>
    </section>
    {error ? <EmptyState title="Tasks could not load" body={error.message} /> : null}
    {!error && !isLoading && filtered.length === 0 ? <EmptyState title="No matching tasks" body="Create a task or change the current course, status, or search filter." action={<Button icon={Plus} onClick={() => setEditing('new')}>New task</Button>} /> : null}
    {isLoading ? <div className="task-list panel"><div className="task-list__loading" /></div> : null}
    {filtered.length ? <section className="task-list panel" aria-label="Tracked tasks">
      {filtered.map((task) => <button className="task-list__row" key={task.logical_id} onClick={() => setEditing(task)}>
        <span className={`task-status-dot ${task.completed ? 'is-complete' : ''}`}>{task.completed ? <CheckCircle2 size={17} /> : null}</span>
        <span className="task-list__main"><small>{task.course.name}</small><strong>{task.display_title}</strong><span>{task.details || 'No description'}</span></span>
        <span className="task-list__due"><CalendarDays size={14} />{task.due_date ? formatTaskDate(task.due_date) : 'No due date'}</span>
        {task.manually_managed ? <span className="manual-badge">Manual</span> : null}
        <Edit3 size={16} />
      </button>)}
    </section> : null}
    {editing ? <TaskEditor
      task={editing === 'new' ? null : editing}
      courses={courses ?? []}
      defaultCourseId={selectedCourseId}
      onClose={() => setEditing(null)}
      onSaved={async (task) => {
        await mutate('/api/v1/tasks')
        setEditing(null)
        toast(task ? 'Task updated in Google Tasks.' : 'Task created in Google Tasks.', 'success')
      }}
    /> : null}
  </div>
}

function TaskEditor({ task, courses, defaultCourseId, onClose, onSaved }: {
  task: TrackedTask | null
  courses: CourseView[]
  defaultCourseId: string | null
  onClose: () => void
  onSaved: (task: TrackedTask | null) => Promise<void>
}) {
  const { toast } = useApp()
  const [form, setForm] = useState<ManualTaskInput>(() => taskToForm(task, defaultCourseId ?? courses[0]?.id ?? ''))
  const [saving, setSaving] = useState(false)
  const update = <K extends keyof ManualTaskInput>(key: K, value: ManualTaskInput[K]) => setForm((current) => ({ ...current, [key]: value }))

  async function save() {
    setSaving(true)
    try {
      const saved = await mutateJson<TrackedTask>(task ? `/api/v1/tasks/${encodeURIComponent(task.logical_id)}` : '/api/v1/tasks', {
        method: task ? 'PUT' : 'POST',
        body: form,
      })
      await onSaved(task ? saved : null)
    } catch (error) {
      toast(error instanceof Error ? error.message : 'The task could not be saved.', 'error')
    } finally {
      setSaving(false)
    }
  }

  return <Modal title={task ? 'Edit task' : 'New task'} onClose={onClose} footer={<><Button variant="secondary" disabled={saving} onClick={onClose}>Cancel</Button><Button disabled={saving || !form.title.trim() || !form.course_id} onClick={() => void save()}>{saving ? 'Saving…' : task ? 'Save changes' : 'Create task'}</Button></>}>
    <div className="task-editor-form">
      <label className="form-field"><span>Task name</span><input value={form.title} onChange={(event) => update('title', event.target.value)} /></label>
      <div className="form-grid form-grid--two">
        <label className="form-field"><span>Course</span><select value={form.course_id} disabled={Boolean(task)} onChange={(event) => update('course_id', event.target.value)}>{courses.map((course) => <option key={course.id} value={course.id}>{course.settings.name}</option>)}</select>{task ? <small>Course and Google task list stay fixed after creation.</small> : null}</label>
        <label className="form-field"><span>Due date</span><input type="date" value={form.due_date ?? ''} onChange={(event) => update('due_date', event.target.value || null)} /></label>
        <label className="form-field"><span>Task type</span><select value={form.task_type} onChange={(event) => update('task_type', event.target.value as ManualTaskInput['task_type'])}><option value="assignment">Assignment</option><option value="quiz">Quiz</option><option value="test">Test</option></select></label>
        <label className="form-field"><span>Classification</span><select value={form.classification} onChange={(event) => update('classification', event.target.value as ManualTaskInput['classification'])}><option value="homework">Homework</option><option value="classwork">Classwork</option></select></label>
        <label className="form-field"><span>Action</span><select value={form.action_kind} onChange={(event) => update('action_kind', event.target.value)}>{ACTIONS.map((action) => <option key={action} value={action}>{action[0]?.toUpperCase()}{action.slice(1)}</option>)}</select></label>
        <label className="form-field"><span>Status</span><select value={form.completed ? 'completed' : 'open'} onChange={(event) => update('completed', event.target.value === 'completed')}><option value="open">Open</option><option value="completed">Completed</option></select></label>
      </div>
      <label className="form-field"><span>Description / notes</span><textarea rows={5} value={form.details} onChange={(event) => update('details', event.target.value)} /></label>
      <label className="form-field"><span>Source URL (optional)</span><input type="url" value={form.source_url ?? ''} onChange={(event) => update('source_url', event.target.value || null)} /></label>
      <label className="form-field"><span>Canvas assignment URL (optional)</span><input type="url" value={form.assignment_url ?? ''} onChange={(event) => update('assignment_url', event.target.value || null)} /></label>
    </div>
  </Modal>
}

function taskToForm(task: TrackedTask | null, courseId: string): ManualTaskInput {
  return {
    course_id: task?.course.id ?? courseId,
    title: task?.display_title ?? '',
    details: task?.details ?? '',
    due_date: task?.due_date ?? null,
    completed: task?.completed === true,
    classification: task?.classification ?? 'homework',
    task_type: task?.task_type ?? 'assignment',
    action_kind: task?.action_kind ?? 'complete',
    source_url: task?.source.url ?? null,
    assignment_url: task?.canvas.assignment_url ?? task?.source.assignment_url ?? null,
  }
}

function formatTaskDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${value}T00:00:00Z`))
}
