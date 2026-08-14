import {
  Activity,
  BookOpen,
  CalendarDays,
  CircleCheck,
  Clock3,
  Grid2X2,
  HeartPulse,
  MoreHorizontal,
  RefreshCw,
  Settings,
} from 'lucide-react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { mutate } from 'swr'
import { mutateJson, useOverview } from '../lib/api'
import { useApp } from './AppContext'
import { CourseSwitcher } from './CourseSwitcher'
import { Button } from './ui'

const navigation = [
  { to: '/', label: 'Overview', icon: Grid2X2, end: true },
  { to: '/runs', label: 'Runs', icon: Clock3 },
  { to: '/courses', label: 'Courses', icon: BookOpen },
  { to: '/schedules', label: 'Schedules', icon: CalendarDays },
  { to: '/diagnostics', label: 'Diagnostics', icon: Activity, desktopOnly: true },
  { to: '/settings', label: 'Settings', icon: Settings, desktopOnly: true },
]

export function AppShell() {
  const { selectedCourseId, setSelectedCourseId, toast } = useApp()
  const { data } = useOverview(selectedCourseId)
  const navigate = useNavigate()
  const location = useLocation()
  const healthy = Boolean(data?.connections.google_authorized && data.connections.gemini_configured)

  async function runHealth() {
    try {
      const result = await mutateJson<{ run_id: number }>(
        `/api/v1/health-runs${selectedCourseId ? `?course_id=${encodeURIComponent(selectedCourseId)}` : ''}`,
      )
      await mutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
      navigate(`/runs/${result.run_id}`)
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Health check could not be started.', 'error')
    }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <NavLink to="/" className="brand" aria-label="Canvas Task Sync overview">
        <RefreshCw size={31} strokeWidth={2.2} /><span>Canvas Task Sync</span>
      </NavLink>
      <nav className="desktop-nav" aria-label="Primary navigation">
        {navigation.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => isActive ? 'nav-item nav-item--active' : 'nav-item'}>
          <Icon size={22} /><span>{label}</span>
        </NavLink>)}
      </nav>
      <div className="server-status"><span className="online-dot" /><div><strong>Local server</strong><span>Connected</span></div></div>
    </aside>
    <div className="app-main">
      <header className="topbar">
        <div className="mobile-brand">
          <NavLink to="/" aria-label="Canvas Task Sync overview"><RefreshCw size={29} strokeWidth={2.2} /><span>Canvas Task Sync</span></NavLink>
          <NavLink to="/settings" aria-label="Open settings"><MoreHorizontal size={25} /></NavLink>
        </div>
        <CourseSwitcher courses={data?.courses ?? []} selectedCourseId={selectedCourseId} onSelect={setSelectedCourseId} />
        <div className="topbar__actions">
          <Button variant="secondary" icon={HeartPulse} onClick={runHealth}>Run health check</Button>
          <div className={`connection-summary ${healthy ? 'tone-success' : 'tone-warning'}`}>
            <CircleCheck size={20} /><span>{healthy ? 'Google + Gemini connected' : 'Setup needs attention'}</span>
          </div>
        </div>
      </header>
      <main className="page-shell" key={location.pathname}><Outlet /></main>
    </div>
    <nav className="mobile-nav" aria-label="Mobile navigation">
      {navigation.filter((item) => !item.desktopOnly).map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => isActive ? 'mobile-nav__item mobile-nav__item--active' : 'mobile-nav__item'}><Icon size={22} /><span>{label}</span></NavLink>)}
      <NavLink to="/settings" className={({ isActive }) => isActive || location.pathname === '/diagnostics' ? 'mobile-nav__item mobile-nav__item--active' : 'mobile-nav__item'}><MoreHorizontal size={22} /><span>More</span></NavLink>
    </nav>
  </div>
}
