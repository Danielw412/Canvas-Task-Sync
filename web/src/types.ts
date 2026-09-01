export type HealthState = 'healthy' | 'warning' | 'error' | 'missing'
export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'applying'
  | 'succeeded'
  | 'review_needed'
  | 'stale'
  | 'cancelled'
  | 'failed'
  | 'failed_partial'

export type RunStage =
  | 'queued'
  | 'validate_configuration'
  | 'authenticate_services'
  | 'capture_source'
  | 'extract_assignments'
  | 'calculate_deadlines'
  | 'compare_google_tasks'
  | 'build_review_plan'
  | 'revalidate_preview'
  | 'apply_changes'
  | 'persist_state'
  | 'health_check'
  | 'complete'

export type ExtractionMode = 'image' | 'text' | 'hybrid' | 'auto'
export type WeekSelection = 'previous_week' | 'this_week' | 'next_week'
export type AcquisitionStrategy = 'auto' | 'canvas_api' | 'configured_source'
export type GeminiModel =
  | 'gemini-3.7-flash'
  | 'gemini-3.6-flash'
  | 'gemini-3.5-flash'
  | 'gemini-3.5-flash-lite'
export type GeminiReasoning = 'low' | 'medium' | 'high'
export type BrowserSourceFormat = 'auto' | 'google_slides' | 'google_docs' | 'google_sheets'
export type SyncActionKind =
  | 'create'
  | 'update'
  | 'unchanged'
  | 'uncertain'
  | 'ignored'
  | 'source_missing'
  | 'remote_missing'
  | 'historical_blocked'
  | 'notes_cleanup'

export interface ExtractionSettings {
  mode: ExtractionMode
  thumbnail_size: 'small' | 'medium' | 'large'
  assignments_default_due: 'explicit_date' | 'same_day' | 'next_class' | 'none'
  same_day_action_kinds: string[]
}

export interface GoogleSlidesSourceSettings {
  type: 'google_slides'
  url: string
  page_id: string
  extraction: ExtractionSettings
}

export interface BrowserSourceSettings {
  type: 'browser'
  url: string
  source_format: BrowserSourceFormat
  freshness_seconds: number
  selection: {
    slide_ids: string[]
    section_ids: string[]
    sheets: { sheet_id?: string | null; sheet_name?: string | null; range_a1?: string | null }[]
  }
  extraction: ExtractionSettings
}

export interface NoFallbackSourceSettings {
  type: 'none'
  extraction: ExtractionSettings
}

export interface CourseSettings {
  enabled: boolean
  name: string
  prefix: string
  task_list: string
  assessment_task_list: string
  ai_instructions: string
  gemini_model?: GeminiModel | null
  gemini_fallback_models?: GeminiModel[] | null
  gemini_reasoning: GeminiReasoning
  timezone: string
  meeting_days: string[]
  canvas_course_id?: string | null
  canvas_base_url?: string | null
  source: GoogleSlidesSourceSettings | BrowserSourceSettings | NoFallbackSourceSettings
}

export interface CourseView {
  id: string
  settings: CourseSettings
  readiness: HealthState
  readiness_message: string
}

export interface ConnectionItem {
  key: string
  label: string
  state: HealthState
  summary: string
  details?: string | null
  checked_at?: string | null
}

export interface ConnectionStatus {
  google_client_configured: boolean
  google_authorized: boolean
  gemini_configured: boolean
  local_server: string
  checks: ConnectionItem[]
}

export interface SyncAction {
  kind: SyncActionKind
  title: string
  logical_id?: string | null
  due_date?: string | null
  due_uncertain?: boolean
  due_uncertain_reason?: string | null
  reason: string
  evidence?: string | null
  source_anchor?: string | null
  remote_task_id?: string | null
  task_list?: string | null
  due_verified?: boolean
  desired?: {
    details: string
    source_url: string
    assignment_url?: string | null
    source_text: string
    due_basis: string
    action_kind: string
    classification: string
    task_type: 'assignment' | 'quiz' | 'test'
    destination_task_list: string
  } | null
}

export interface SyncPlan {
  course_id: string
  task_list: string
  task_lists: string[]
  dry_run: boolean
  extraction_mode: ExtractionMode
  fallback_reasons: string[]
  actions: SyncAction[]
}

export interface RunEvent {
  id: number
  run_id: number
  sequence: number
  created_at: string
  stage: RunStage
  event_type: string
  level: 'info' | 'warning' | 'error'
  message: string
  metadata: Record<string, unknown>
  duration_ms?: number | null
}

export interface RunSummary {
  id: number
  operation_id?: string
  course_id: string
  course_name?: string | null
  trigger: 'manual' | 'schedule'
  requested_mode: 'preview' | 'auto_apply' | 'health'
  status: RunStatus
  stage: RunStage
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  extraction_mode?: ExtractionMode | null
  week_selection?: WeekSelection
  target_week_start?: string | null
  acquisition_strategy?: AcquisitionStrategy
  counts: Record<string, number>
  applied_counts: Record<string, number>
  plan_hash?: string | null
  error_code?: string | null
  error_summary?: string | null
  schedule_id?: number | null
}

export interface OperationSummary {
  id: string
  run_ids: number[]
  course_ids: string[]
  course_names: string[]
  status: RunStatus
  created_at: string
  finished_at?: string | null
}

export interface OperationLogEvent extends RunEvent {
  operation_id: string
  course_id: string
  course_name: string
}

export interface RunDetail extends RunSummary {
  include_past: boolean
  test_rebase_week?: string | null
  config_hash?: string | null
  page_hash?: string | null
  remote_hash?: string | null
  cancel_requested: boolean
  plan?: SyncPlan | null
  events: RunEvent[]
}

export interface Schedule {
  id: number
  name: string
  course_id: string
  weekdays: number[]
  local_time: string
  timezone: string
  mode: 'preview' | 'auto_apply'
  enabled: boolean
  next_run_at?: string | null
  last_run_at?: string | null
  last_result?: string | null
  created_at: string
  updated_at: string
}

export interface ScheduleOccurrence {
  id: number
  schedule_id: number
  scheduled_for: string
  status: string
  run_id?: number | null
  details: string
  created_at: string
}

export interface OverviewResponse {
  selected_course_id?: string | null
  courses: CourseView[]
  connections: ConnectionStatus
  latest_run?: RunSummary | null
  recent_runs: RunSummary[]
  next_schedule?: Schedule | null
}

export interface TrackedTask {
  logical_id: string
  course: {
    id: string
    name: string
    prefix: string
  }
  title: string
  display_title: string
  details: string
  due_date?: string | null
  completed: boolean | null
  completion_status: string
  classification?: 'homework' | 'classwork' | null
  task_type?: 'assignment' | 'quiz' | 'test' | null
  action_kind?: string | null
  manually_managed: boolean
  google_task: {
    task_id?: string | null
    tasklist_title?: string | null
    status: string
  }
  source: {
    type: string
    url?: string | null
    assignment_url?: string | null
  }
  canvas: {
    assignment_url?: string | null
  }
}

export interface ManualTaskInput {
  course_id: string
  title: string
  details: string
  due_date: string | null
  completed: boolean
  classification: 'homework' | 'classwork'
  task_type: 'assignment' | 'quiz' | 'test'
  action_kind: string
  source_url: string | null
  assignment_url: string | null
}

export interface DiagnosticsResponse {
  checks: ConnectionItem[]
  recent_events: RunEvent[]
  error_runs: RunSummary[]
  control_database: string
  state_database: string
}

export interface ApiErrorShape {
  error: {
    code: string
    message: string
    retryable: boolean
    field_errors?: Record<string, string[]> | null
    run_id?: number | null
  }
}
