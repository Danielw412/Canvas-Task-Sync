import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TasksPage from './TasksPage'

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('../components/AppContext', () => ({
  useApp: () => ({ selectedCourseId: 'physics', toast }),
}))

const course = {
  id: 'physics',
  readiness: 'healthy',
  readiness_message: 'Ready',
  settings: {
    enabled: true,
    name: 'AP Physics C',
    prefix: 'PHYSICS',
    task_list: 'School',
    assessment_task_list: 'Tests',
    ai_instructions: '',
    gemini_reasoning: 'medium',
    timezone: 'America/New_York',
    meeting_days: ['mon'],
    source: { type: 'none', extraction: { mode: 'text', thumbnail_size: 'large', assignments_default_due: 'next_class', same_day_action_kinds: [] } },
  },
}

const task = {
  logical_id: 'logical-1',
  course: { id: 'physics', name: 'AP Physics C', prefix: 'PHYSICS' },
  title: '[PHYSICS] Existing task',
  display_title: 'Existing task',
  details: 'Original notes',
  due_date: '2026-09-05',
  completed: false,
  completion_status: 'incomplete',
  classification: 'homework',
  task_type: 'assignment',
  action_kind: 'complete',
  manually_managed: false,
  google_task: { task_id: 'google-1', tasklist_title: 'School', status: 'needsAction' },
  source: { type: 'canvas', url: null, assignment_url: null },
  canvas: { assignment_url: null },
}

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('TasksPage', () => {
  beforeEach(() => {
    toast.mockReset()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/v1/tasks' && !init?.method) return response([task])
      if (url === '/api/v1/courses') return response([course])
      if (url === '/api/v1/bootstrap') return response({ csrf_token: 'csrf-token' })
      if (url === '/api/v1/tasks' && init?.method === 'POST') {
        return response({ ...task, logical_id: 'manual-1', display_title: 'New reading', manually_managed: true }, 201)
      }
      if (url === '/api/v1/tasks/logical-1' && init?.method === 'PUT') {
        return response({ ...task, display_title: 'Edited task', manually_managed: true })
      }
      throw new Error(`Unexpected request: ${url}`)
    }))
  })

  function renderPage() {
    return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TasksPage /></SWRConfig>)
  }

  it('creates and edits Google-backed tasks', async () => {
    renderPage()
    await screen.findByText('Existing task')

    fireEvent.click(screen.getByRole('button', { name: 'New task' }))
    fireEvent.change(screen.getByLabelText('Task name'), { target: { value: 'New reading' } })
    fireEvent.change(screen.getByLabelText('Description / notes'), { target: { value: 'Read pages 40–52.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/tasks', expect.objectContaining({ method: 'POST' })))

    fireEvent.click(await screen.findByText('Existing task'))
    const name = screen.getByLabelText('Task name')
    fireEvent.change(name, { target: { value: 'Edited task' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/tasks/logical-1', expect.objectContaining({ method: 'PUT' })))
  })
})
