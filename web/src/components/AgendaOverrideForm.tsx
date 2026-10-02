import type { CourseSettings } from '../types'

function calendarDate(timezone: string): string {
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

function offsetDate(value: string, days: number): string {
  const date = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(date.getTime())) return ''
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

export function AgendaOverrideForm({ settings, update }: {
  settings: CourseSettings
  update: (recipe: (value: CourseSettings) => void) => void
}) {
  const override = settings.canvas_agenda_override
  const today = calendarDate(settings.timezone)
  const expiresOn = override ? offsetDate(override.target_week_start, 6) : ''
  const expired = Boolean(expiresOn && today > expiresOn)

  function toggle(enabled: boolean) {
    update((value) => {
      const weekday = new Date(`${today}T00:00:00Z`).getUTCDay()
      const monday = offsetDate(today, -((weekday + 6) % 7))
      value.canvas_agenda_override = enabled ? {
        page_slug: '',
        table_number: 1,
        expected_heading_date: offsetDate(monday, -7),
        target_week_start: monday,
        required_text: '',
      } : null
    })
  }

  return <div className="form-section">
    <h3>Temporary agenda override</h3>
    <p className="form-help">Use this when a Canvas agenda table has the wrong week heading. It applies only to this course and the selected week. Assignment and exam dates stay as written.</p>
    <label className="agenda-override-toggle"><input type="checkbox" checked={Boolean(override)} onChange={(event) => toggle(event.target.checked)} />Use a temporary agenda override</label>
    {override ? <>
      <div className="form-grid form-grid--two">
        <label className="form-field"><span>Target week starting Monday</span><input type="date" value={override.target_week_start} onChange={(event) => update((value) => { value.canvas_agenda_override!.target_week_start = event.target.value })} /><small>The intended week for this table.</small></label>
        <label className="form-field"><span>Heading date on Canvas</span><input type="date" value={override.expected_heading_date} onChange={(event) => update((value) => { value.canvas_agenda_override!.expected_heading_date = event.target.value })} /><small>The date currently shown in its week heading.</small></label>
        <label className="form-field"><span>Agenda page slug</span><input value={override.page_slug} maxLength={255} placeholder="weekly-agenda" onChange={(event) => update((value) => { value.canvas_agenda_override!.page_slug = event.target.value })} /><small>Copy the part of the Canvas page URL after /pages/.</small></label>
        <label className="form-field"><span>Agenda table number</span><input type="number" min={1} max={100} value={override.table_number || ''} onChange={(event) => update((value) => { value.canvas_agenda_override!.table_number = Number(event.target.value) })} /><small>Count agenda tables from the top: 1 is the first. Layout tables are excluded.</small></label>
      </div>
      <label className="form-field"><span>Confirmation text</span><input value={override.required_text} maxLength={200} placeholder="A distinctive phrase from this table" onChange={(event) => update((value) => { value.canvas_agenda_override!.required_text = event.target.value })} /><small>Choose text that appears in this agenda table only. If the table or heading no longer matches, the run stops for review.</small></label>
      <p className={expired ? 'form-help tone-warning' : 'form-help'}>{expired ? 'Expired. Normal agenda discovery is in use.' : expiresOn ? `Expires after ${expiresOn} in ${settings.timezone}. Runs for other weeks use normal agenda discovery.` : 'Select a target week to set its expiry.'}</p>
    </> : null}
  </div>
}
