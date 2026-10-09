import { agendaOverrideExpiry, calendarDate, offsetDate } from '../lib/agenda-override'
import type { CourseSettings } from '../types'
import { Field } from './ui'

export function AgendaOverrideForm({ settings, update }: {
  settings: CourseSettings
  update: (recipe: (value: CourseSettings) => void) => void
}) {
  const override = settings.canvas_agenda_override
  const today = calendarDate(settings.timezone)
  const expiry = agendaOverrideExpiry(settings)

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

  return <div className="form-panel">
    <div className="form-panel__head"><h3>Temporary agenda override</h3>{override && expiry && !expiry.expired ? <span className="badge badge--info">Active</span> : null}</div>
    <p className="field__help">For when a Canvas agenda table carries the wrong week heading. It applies to this course and one week only. Assignment and exam dates stay as written.</p>
    <label className="check">
      <input type="checkbox" checked={Boolean(override)} onChange={(event) => toggle(event.target.checked)} />
      <span>Use a temporary agenda override</span>
    </label>
    {override ? <>
      <div className="form-grid">
        <Field label="Target week starting Monday" help="The week this table is really for.">
          <input className="control" type="date" value={override.target_week_start} onChange={(event) => update((value) => { value.canvas_agenda_override!.target_week_start = event.target.value })} />
        </Field>
        <Field label="Heading date on Canvas" help="The date its week heading shows now.">
          <input className="control" type="date" value={override.expected_heading_date} onChange={(event) => update((value) => { value.canvas_agenda_override!.expected_heading_date = event.target.value })} />
        </Field>
        <Field label="Agenda page slug" help="The part of the Canvas page address after /pages/.">
          <input className="control control--mono" value={override.page_slug} maxLength={255} placeholder="weekly-agenda" onChange={(event) => update((value) => { value.canvas_agenda_override!.page_slug = event.target.value })} />
        </Field>
        <Field label="Agenda table number" help="Counting agenda tables from the top, starting at 1. Layout tables don't count.">
          <input className="control" type="number" min={1} max={100} value={override.table_number || ''} onChange={(event) => update((value) => { value.canvas_agenda_override!.table_number = Number(event.target.value) })} />
        </Field>
      </div>
      <Field label="Confirmation text" help="A phrase that appears only in this table. If the table or heading stops matching, the run stops for review.">
        <input className="control" value={override.required_text} maxLength={200} placeholder="A distinctive phrase from this table" onChange={(event) => update((value) => { value.canvas_agenda_override!.required_text = event.target.value })} />
      </Field>
      <p className={expiry?.expired ? 'field__help tone-warning' : 'field__help'}>{expiry?.expired ? 'Expired. Normal agenda discovery is in use.' : expiry?.expiresOn ? `Expires after ${expiry.expiresOn} in ${settings.timezone}. Runs for other weeks use normal agenda discovery.` : 'Select a target week to set its expiry.'}</p>
    </> : null}
  </div>
}
