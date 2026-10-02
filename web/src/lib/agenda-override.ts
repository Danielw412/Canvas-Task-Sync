import type { CourseSettings } from '../types'

export function agendaOverrideValidationMessage(settings: CourseSettings): string | null {
  const override = settings.canvas_agenda_override
  if (!override) return null
  if (!settings.canvas_course_id) return 'A temporary agenda override requires a Canvas course ID.'
  if (!/^[A-Za-z0-9_-]+$/.test(override.page_slug.trim())) {
    return 'Enter the agenda page slug: the part of its Canvas URL after /pages/.'
  }
  if (!override.target_week_start || !override.expected_heading_date || !override.required_text.trim()) {
    return 'Complete both override dates and the confirmation text before saving.'
  }
  if (new Date(`${override.target_week_start}T00:00:00Z`).getUTCDay() !== 1) {
    return 'The override target week must begin on a Monday.'
  }
  if (!Number.isInteger(override.table_number) || override.table_number < 1 || override.table_number > 100) {
    return 'Choose an agenda table number from 1 to 100.'
  }
  return null
}
