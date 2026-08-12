import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { useOverview } from '../lib/api'

interface ToastMessage {
  id: number
  tone: 'success' | 'warning' | 'error' | 'info'
  message: string
}

interface AppContextValue {
  selectedCourseId: string | null
  setSelectedCourseId: (value: string) => void
  toast: (message: string, tone?: ToastMessage['tone']) => void
}

const AppContext = createContext<AppContextValue | null>(null)

export function AppProvider({ children }: { children: ReactNode }) {
  const [courseSelection, setCourseSelection] = useState<string | null>(null)
  const [toasts, setToasts] = useState<ToastMessage[]>([])
  const { data } = useOverview(courseSelection)
  const selectedCourseId = courseSelection ?? data?.selected_course_id ?? null
  const toast = useCallback((message: string, tone: ToastMessage['tone'] = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { id, tone, message }])
    window.setTimeout(() => setToasts((current) => current.filter((item) => item.id !== id)), 4_500)
  }, [])
  const value = useMemo(
    () => ({ selectedCourseId, setSelectedCourseId: setCourseSelection, toast }),
    [selectedCourseId, toast],
  )
  return (
    <AppContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((item) => <div className={`toast toast--${item.tone}`} key={item.id}>{item.message}</div>)}
      </div>
    </AppContext.Provider>
  )
}

// The provider and hook intentionally share this small module.
// eslint-disable-next-line react-refresh/only-export-components
export function useApp() {
  const value = useContext(AppContext)
  if (!value) throw new Error('useApp must be used inside AppProvider')
  return value
}
