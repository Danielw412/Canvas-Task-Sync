import {
  BooksIcon,
  CalendarBlankIcon,
  CheckCircleIcon,
  ClockCounterClockwiseIcon,
  DotsThreeOutlineIcon,
  GearSixIcon,
  HouseSimpleIcon,
  ListChecksIcon,
  PulseIcon,
  WarningCircleIcon,
} from '@phosphor-icons/react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import logoUrl from '../assets/logo.png'
import { useOverview } from '../lib/api'
import { useApp } from './AppContext'
import { Menu } from './ui'

const navigation = [
  { to: '/', label: 'Overview', icon: HouseSimpleIcon, end: true },
  { to: '/tasks', label: 'Tasks', icon: ListChecksIcon },
  { to: '/runs', label: 'Runs', icon: ClockCounterClockwiseIcon },
  { to: '/courses', label: 'Courses', icon: BooksIcon },
  { to: '/schedules', label: 'Schedules', icon: CalendarBlankIcon, mobileMore: true },
]

const moreRoutes = ['/schedules', '/diagnostics', '/settings']

export function AppShell() {
  const { selectedCourseId } = useApp()
  const { data } = useOverview(selectedCourseId)
  const navigate = useNavigate()
  const location = useLocation()
  const ready = Boolean(data?.connections.google_authorized && data.connections.extraction_ready)

  return <div className="shell">
    <a className="skip-link" href="#main">Skip to content</a>
    <header className="topbar">
      <div className="topbar__inner">
        <Link to="/" className="brand" aria-label="Canvas Task Sync overview">
          <img src={logoUrl} alt="" />
          <span>Canvas Task Sync</span>
        </Link>
        <nav className="nav" aria-label="Primary navigation">
          {navigation.map(({ to, label, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => `nav__link${isActive ? ' is-active' : ''}`}>{label}</NavLink>)}
        </nav>
        <div className="topbar__end">
          {data ? ready
            ? <Link to="/diagnostics" className="health-link" title="Google and the extraction agent are connected. Open diagnostics.">
              <CheckCircleIcon className="tone-success" size={17} weight="fill" aria-hidden /><span className="health-link__text">All systems ready</span>
            </Link>
            : <Link to="/settings" className="health-link health-link--warning" title="A connection needs setup. Open settings.">
              <WarningCircleIcon size={17} weight="fill" aria-hidden /><span className="health-link__text">Finish setup</span>
            </Link> : null}
          <NavLink to="/settings" className={({ isActive }) => `nav__link settings-link${isActive ? ' is-active' : ''}`} aria-label="Settings" title="Settings">
            <GearSixIcon size={19} aria-hidden /><span>Settings</span>
          </NavLink>
        </div>
      </div>
    </header>

    <main className="page-shell" id="main" key={location.pathname}><Outlet /></main>

    <nav className="mobile-nav" aria-label="Mobile navigation">
      {navigation.filter((item) => !item.mobileMore).map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => `mobile-nav__item${isActive ? ' is-active' : ''}`}>
        <Icon size={22} aria-hidden /><span>{label}</span>
      </NavLink>)}
      <Menu
        label="More pages"
        placement="up"
        triggerClassName={`mobile-nav__item${moreRoutes.includes(location.pathname) ? ' is-active' : ''}`}
        trigger={<><DotsThreeOutlineIcon size={22} aria-hidden /><span>More</span></>}
        items={[
          { label: 'Schedules', icon: CalendarBlankIcon, onSelect: () => navigate('/schedules') },
          { label: 'Diagnostics', icon: PulseIcon, onSelect: () => navigate('/diagnostics') },
          { label: 'Settings', icon: GearSixIcon, onSelect: () => navigate('/settings') },
        ]}
      />
    </nav>
  </div>
}
