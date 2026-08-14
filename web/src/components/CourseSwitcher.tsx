import { BookOpen, Check, ChevronDown } from 'lucide-react'
import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import type { CourseView } from '../types'

interface CourseSwitcherProps {
  courses: CourseView[]
  selectedCourseId: string | null
  onSelect: (courseId: string) => void
}

const readinessLabels: Record<CourseView['readiness'], string> = {
  healthy: 'Ready to sync',
  warning: 'Needs attention',
  error: 'Connection error',
  missing: 'Setup incomplete',
}

export function CourseSwitcher({ courses, selectedCourseId, onSelect }: CourseSwitcherProps) {
  const [open, setOpen] = useState(false)
  const menuId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([])
  const selectedIndex = Math.max(0, courses.findIndex((course) => course.id === selectedCourseId))
  const selectedCourse = courses[selectedIndex]

  useEffect(() => {
    if (!open) return

    function closeOnOutsidePointer(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsidePointer)
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer)
  }, [open])

  useEffect(() => {
    if (open) optionRefs.current[selectedIndex]?.focus()
  }, [open, selectedIndex])

  function closeAndRestoreFocus() {
    setOpen(false)
    triggerRef.current?.focus()
  }

  function handleTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    setOpen(true)
  }

  function handleOptionKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === 'Escape') {
      event.preventDefault()
      closeAndRestoreFocus()
      return
    }
    if (event.key === 'Tab') {
      setOpen(false)
      return
    }

    let nextIndex: number | null = null
    if (event.key === 'ArrowDown') nextIndex = (index + 1) % courses.length
    if (event.key === 'ArrowUp') nextIndex = (index - 1 + courses.length) % courses.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = courses.length - 1
    if (nextIndex == null) return

    event.preventDefault()
    optionRefs.current[nextIndex]?.focus()
  }

  function selectCourse(courseId: string) {
    onSelect(courseId)
    closeAndRestoreFocus()
  }

  return (
    <div className={`course-switcher ${open ? 'is-open' : ''}`} ref={rootRef}>
      <button
        ref={triggerRef}
        className="course-switcher__trigger"
        type="button"
        aria-label={`Selected course: ${selectedCourse?.settings.name ?? 'Loading courses'}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={!courses.length}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="course-switcher__icon" aria-hidden="true"><BookOpen size={19} /></span>
        <span className="course-switcher__selection">
          <span>Current course</span>
          <strong>{selectedCourse?.settings.name ?? 'Loading courses...'}</strong>
        </span>
        <ChevronDown className="course-switcher__chevron" aria-hidden="true" size={17} />
      </button>

      {open ? <div className="course-switcher__menu">
        <header className="course-switcher__menu-header">
          <span>Switch course</span>
          <small>{courses.length} {courses.length === 1 ? 'course' : 'courses'}</small>
        </header>
        <div className="course-switcher__options" id={menuId} role="listbox" aria-label="Courses">
          {courses.map((course, index) => {
            const selected = course.id === selectedCourse?.id
            return <button
              key={course.id}
              ref={(element) => { optionRefs.current[index] = element }}
              className={`course-switcher__option ${selected ? 'is-selected' : ''}`}
              type="button"
              role="option"
              aria-selected={selected}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectCourse(course.id)}
              onKeyDown={(event) => handleOptionKeyDown(event, index)}
            >
              <span className={`course-switcher__status course-switcher__status--${course.readiness}`} aria-hidden="true" />
              <span className="course-switcher__option-copy">
                <strong>{course.settings.name}</strong>
                <small>{course.readiness_message || readinessLabels[course.readiness]}</small>
              </span>
              {selected ? <span className="course-switcher__check" aria-hidden="true"><Check size={15} strokeWidth={2.6} /></span> : null}
            </button>
          })}
        </div>
      </div> : null}
    </div>
  )
}
