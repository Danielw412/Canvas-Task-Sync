import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { CourseView } from '../types'
import { CourseSwitcher } from './CourseSwitcher'

const settings: CourseView['settings'] = {
  enabled: true,
  name: 'Honors Spanish IV',
  prefix: 'SPANISH',
  task_list: 'School',
  assessment_task_list: 'Tests',
  ai_instructions: '',
  timezone: 'America/New_York',
  meeting_days: ['mon', 'tue', 'wed', 'thu', 'fri'],
  source: {
    type: 'google_slides',
    url: 'https://docs.google.com/presentation/d/example/edit',
    page_id: 'slide-1',
    extraction: {
      mode: 'hybrid',
      thumbnail_size: 'large',
      assignments_default_due: 'next_class',
      same_day_action_kinds: ['submit'],
    },
  },
}

const courses: CourseView[] = [
  { id: 'spanish', settings, readiness: 'healthy', readiness_message: 'Ready' },
  {
    id: 'history',
    settings: { ...settings, name: 'AP World History', prefix: 'HISTORY' },
    readiness: 'warning',
    readiness_message: 'Source needs attention',
  },
]

describe('CourseSwitcher', () => {
  it('shows a styled listbox and selects another course', () => {
    const onSelect = vi.fn()
    render(<CourseSwitcher courses={courses} selectedCourseId="spanish" onSelect={onSelect} />)

    const trigger = screen.getByRole('button', { name: 'Selected course: Honors Spanish IV' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(trigger)

    const listbox = screen.getByRole('listbox', { name: 'Courses' })
    const options = within(listbox).getAllByRole('option')
    expect(options).toHaveLength(2)
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    expect(options[0]).toHaveFocus()

    fireEvent.keyDown(options[0], { key: 'ArrowDown' })
    expect(options[1]).toHaveFocus()
    fireEvent.click(options[1])

    expect(onSelect).toHaveBeenCalledWith('history')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('closes with Escape and returns focus to the trigger', () => {
    render(<CourseSwitcher courses={courses} selectedCourseId="spanish" onSelect={vi.fn()} />)

    const trigger = screen.getByRole('button', { name: 'Selected course: Honors Spanish IV' })
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    const selectedOption = within(screen.getByRole('listbox')).getByRole('option', { name: /Honors Spanish IV/ })
    fireEvent.keyDown(selectedOption, { key: 'Escape' })

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })
})
