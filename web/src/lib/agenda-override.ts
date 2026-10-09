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

export function calendarDate(timezone: string): string {
  // A timezone draft can be incomplete while the user edits the course.
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: timezone })
  } catch {
    timezone = 'UTC'
  }
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const part = (type: string) => parts.find((item) => item.type === type)?.value
  return `${part('year')}-${part('month')}-${part('day')}`
}

export function offsetDate(value: string, days: number): string {
  const date = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(date.getTime())) return ''
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

// An override covers its target week only; after that week, normal discovery resumes.
export function agendaOverrideExpiry(settings: CourseSettings): { expiresOn: string; expired: boolean } | null {
  const override = settings.canvas_agenda_override
  if (!override) return null
  const expiresOn = offsetDate(override.target_week_start, 6)
  return { expiresOn, expired: Boolean(expiresOn && calendarDate(settings.timezone) > expiresOn) }
}
