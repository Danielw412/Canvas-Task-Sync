import { CaretRightIcon, CheckIcon, ListChecksIcon, MagnifyingGlassIcon, PlusIcon, XCircleIcon } from '@phosphor-icons/react'
import { useMemo, useState } from 'react'
import useSWR, { mutate } from 'swr'
import { useApp } from '../components/AppContext'
import { Button, Disclosure, EmptyState, Field, PageHeader, Segmented, Sheet, SkeletonRows } from '../components/ui'
import { dueDayDifference, fetchJson, formatDueDate, mutateJson } from '../lib/api'
import type { CourseView, ManualTaskInput, TrackedTask } from '../types'

type TaskFilter = 'open' | 'completed' | 'all'
type Bucket = 'past' | 'today' | 'week' | 'later' | 'none'

const ACTIONS = ['complete', 'practice', 'bring', 'present', 'submit', 'read', 'study', 'write', 'other']
const BUCKET_ORDER: Bucket[] = ['past', 'today', 'week', 'later', 'none']

function bucketFor(task: TrackedTask): Bucket {
  if (!task.due_date) return 'none'
  const days = dueDayDifference(task.due_date)
  if (days < 0) return 'past'
  if (days === 0) return 'today'
  if (days <= 7) return 'week'
  return 'later'
}

function bucketLabel(bucket: Bucket, filter: TaskFilter) {
  if (bucket === 'past') return filter === 'open' ? 'Overdue' : 'Earlier'
  return { today: 'Today', week: 'Next 7 days', later: 'Later', none: 'No due date' }[bucket]
}

export default function TasksPage() {
  const { selectedCourseId, toast } = useApp()
  const { data: tasks, error, isLoading } = useSWR<TrackedTask[]>('/api/v1/tasks', fetchJson)
  const { data: courses } = useSWR<CourseView[]>('/api/v1/courses', fetchJson)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<TaskFilter>('open')
  const [courseFilter, setCourseFilter] = useState('')
  const [editing, setEditing] = useState<TrackedTask | 'new' | null>(null)

  const counts = useMemo(() => {
    const scoped = (tasks ?? []).filter((task) => !courseFilter || task.course.id === courseFilter)
    return {
      open: scoped.filter((task) => task.completed === false).length,
      completed: scoped.filter((task) => task.completed === true).length,
      all: scoped.length,
    }
  }, [tasks, courseFilter])

  const groups = useMemo(() => {
    const term = query.toLocaleLowerCase()
    const filtered = (tasks ?? []).filter((task) => {
      if (courseFilter && task.course.id !== courseFilter) return false
      if (filter === 'open' && task.completed !== false) return false
      if (filter === 'completed' && task.completed !== true) return false
      return !term || `${task.display_title} ${task.details} ${task.course.name}`.toLocaleLowerCase().includes(term)
    })
    const sorted = [...filtered].sort((a, b) => (a.due_date ?? '9999').localeCompare(b.due_date ?? '9999'))
    return BUCKET_ORDER
      .map((bucket) => ({ bucket, items: sorted.filter((task) => bucketFor(task) === bucket) }))
      .filter((group) => group.items.length)
  }, [courseFilter, filter, query, tasks])

  return <div className="page--tasks tasks-page">
    <PageHeader
      title="Tasks"
      description="Everything synced to Google Tasks, plus tasks you add yourself."
      actions={<Button icon={PlusIcon} disabled={!courses?.length} onClick={() => setEditing('new')}>New task</Button>}
    />
    <div className="toolbar">
      <label className="search">
        <MagnifyingGlassIcon size={16} aria-hidden />
        <input className="control" aria-label="Search tasks" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search tasks" />
      </label>
      <select className="control" aria-label="Course filter" value={courseFilter} onChange={(event) => setCourseFilter(event.target.value)}>
        <option value="">All courses</option>
        {courses?.map((course) => <option value={course.id} key={course.id}>{course.settings.name}</option>)}
      </select>
      <div className="toolbar__end">
        <Segmented
          label="Task status filter"
          value={filter}
          onChange={setFilter}
          options={[
            { value: 'open', label: 'Open', count: counts.open },
            { value: 'completed', label: 'Completed', count: counts.completed },
            { value: 'all', label: 'All', count: counts.all },
          ]}
        />
      </div>
    </div>

    {error ? <div className="surface"><EmptyState icon={XCircleIcon} title="Tasks could not load" body={error.message} /></div> : null}
    {isLoading ? <SkeletonRows rows={5} label="Loading tasks" /> : null}
    {!error && !isLoading && groups.length === 0 ? <div className="surface">
      <EmptyState
        icon={ListChecksIcon}
        title={tasks?.length ? 'No matching tasks' : 'No tasks yet'}
        body={tasks?.length ? 'Change the course, status, or search to see more.' : 'Sync a course to bring in its agenda, or add a task yourself.'}
        action={<Button icon={PlusIcon} disabled={!courses?.length} onClick={() => setEditing('new')}>New task</Button>}
      />
    </div> : null}

    {groups.map(({ bucket, items }) => <section className="group" key={bucket} aria-label={bucketLabel(bucket, filter)}>
      <h2 className={`group-label${bucket === 'past' && filter === 'open' ? ' group-label--danger' : ''}`}>{bucketLabel(bucket, filter)}<span>{items.length}</span></h2>
      <div className="surface">
        {items.map((task) => {
          const overdue = bucket === 'past' && task.completed === false
          return <button type="button" className={`row row--interactive task-row${task.completed ? ' is-done' : ''}`} key={task.logical_id} onClick={() => setEditing(task)}>
            <span className={`task-row__check${task.completed ? ' is-done' : ''}`} aria-hidden>{task.completed ? <CheckIcon size={12} weight="bold" /> : null}</span>
            <span className="task-row__main">
              <span className="row-title">{task.completed ? <span className="sr-only">Completed: </span> : null}{task.display_title}</span>
              <span className="row-meta truncate">{task.course.name}{task.details ? ` · ${task.details}` : ''}</span>
            </span>
            <span className="task-row__meta">
              {task.task_type && task.task_type !== 'assignment' ? <span className="badge badge--accent">{task.task_type === 'quiz' ? 'Quiz' : 'Test'}</span> : null}
              {task.manually_managed ? <span className="badge badge--outline">Manual</span> : null}
              <span className={`task-row__due${overdue ? ' is-overdue' : ''}`}>{task.due_date ? formatDueDate(task.due_date) : 'No date'}</span>
            </span>
            <CaretRightIcon className="row-chevron" size={15} aria-hidden />
          </button>
        })}
      </div>
    </section>)}

    {editing ? <TaskEditor
      task={editing === 'new' ? null : editing}
      courses={courses ?? []}
      defaultCourseId={courseFilter || selectedCourseId}
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

  const hasLinks = Boolean(form.source_url || form.assignment_url)

  return <Sheet
    title={task ? 'Edit task' : 'New task'}
    description={task ? `${task.course.name}, in ${task.google_task.tasklist_title ?? 'Google Tasks'}` : 'Saved straight to Google Tasks.'}
    onClose={onClose}
    footer={<>
      <span className="sheet__footer-spacer" />
      <Button variant="ghost" disabled={saving} onClick={onClose}>Cancel</Button>
      <Button loading={saving} disabled={!form.title.trim() || !form.course_id} onClick={() => void save()}>{task ? 'Save changes' : 'Create task'}</Button>
    </>}
  >
    <Field label="Task name"><input className="control" value={form.title} onChange={(event) => update('title', event.target.value)} /></Field>
    <div className="form-grid">
      <Field label="Course" help={task ? 'Fixed after creation, along with its task list.' : undefined}>
        <select className="control" value={form.course_id} disabled={Boolean(task)} onChange={(event) => update('course_id', event.target.value)}>
          {courses.map((course) => <option key={course.id} value={course.id}>{course.settings.name}</option>)}
        </select>
      </Field>
      <Field label="Due date"><input className="control" type="date" value={form.due_date ?? ''} onChange={(event) => update('due_date', event.target.value || null)} /></Field>
    </div>
    <div className="field">
      <span className="field__label">Status</span>
      <Segmented label="Task status" value={form.completed ? 'completed' : 'open'} onChange={(value) => update('completed', value === 'completed')} options={[{ value: 'open', label: 'Open' }, { value: 'completed', label: 'Completed' }]} />
    </div>
    <Field label="Description / notes"><textarea className="control" rows={5} value={form.details} onChange={(event) => update('details', event.target.value)} /></Field>
    <div className="form-grid form-grid--3">
      <Field label="Type">
        <select className="control" value={form.task_type} onChange={(event) => update('task_type', event.target.value as ManualTaskInput['task_type'])}>
          <option value="assignment">Assignment</option><option value="quiz">Quiz</option><option value="test">Test</option>
        </select>
      </Field>
      <Field label="Classification">
        <select className="control" value={form.classification} onChange={(event) => update('classification', event.target.value as ManualTaskInput['classification'])}>
          <option value="homework">Homework</option><option value="classwork">Classwork</option>
        </select>
      </Field>
      <Field label="Action">
        <select className="control" value={form.action_kind} onChange={(event) => update('action_kind', event.target.value)}>
          {ACTIONS.map((action) => <option key={action} value={action}>{action[0]?.toUpperCase()}{action.slice(1)}</option>)}
        </select>
      </Field>
    </div>
    <Disclosure title="Links" hint="Optional" defaultOpen={hasLinks}>
      <div className="form-stack">
        <Field label="Source URL"><input className="control" type="url" value={form.source_url ?? ''} onChange={(event) => update('source_url', event.target.value || null)} /></Field>
        <Field label="Canvas assignment URL"><input className="control" type="url" value={form.assignment_url ?? ''} onChange={(event) => update('assignment_url', event.target.value || null)} /></Field>
      </div>
    </Disclosure>
  </Sheet>
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
