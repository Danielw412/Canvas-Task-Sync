import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { AppProvider } from './components/AppContext'
import { PageLoader } from './components/ui'

const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const RunsPage = lazy(() => import('./pages/RunsPage'))
const RunDetailPage = lazy(() => import('./pages/RunDetailPage'))
const CoursesPage = lazy(() => import('./pages/CoursesPage'))
const SchedulesPage = lazy(() => import('./pages/SchedulesPage'))
const DiagnosticsPage = lazy(() => import('./pages/DiagnosticsPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))

export default function App() {
  return (
    <AppProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Suspense fallback={<PageLoader />}><OverviewPage /></Suspense>} />
          <Route path="runs" element={<Suspense fallback={<PageLoader />}><RunsPage /></Suspense>} />
          <Route path="runs/:runId" element={<Suspense fallback={<PageLoader />}><RunDetailPage /></Suspense>} />
          <Route path="courses" element={<Suspense fallback={<PageLoader />}><CoursesPage /></Suspense>} />
          <Route path="schedules" element={<Suspense fallback={<PageLoader />}><SchedulesPage /></Suspense>} />
          <Route path="diagnostics" element={<Suspense fallback={<PageLoader />}><DiagnosticsPage /></Suspense>} />
          <Route path="settings" element={<Suspense fallback={<PageLoader />}><SettingsPage /></Suspense>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </AppProvider>
  )
}
