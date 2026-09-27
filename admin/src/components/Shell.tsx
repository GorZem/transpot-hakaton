import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router-dom'

export function Shell({ children, connected = true }: { children: ReactNode; connected?: boolean }) {
  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand" aria-label="ВайбКлоддеры — на главную">
          <b>ВайбКлоддеры</b>
          <span className="lights" aria-hidden><i /><i /><i /></span>
        </Link>
        {!connected && <span className="conn">нет связи с центром</span>}
        <nav className="nav">
          <NavLink to="/monitoring" className={({ isActive }) => (isActive ? 'on' : '')}>Мониторинг</NavLink>
          <NavLink to="/stats" className={({ isActive }) => (isActive ? 'on' : '')}>Статистика</NavLink>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? 'on' : '')}>Настройки</NavLink>
        </nav>
      </header>
      {children}
    </div>
  )
}
