import type { SiteSummary } from '../api'

/** Список объектов для боковой панели: точка статуса и название. */
export function SiteList({ sites, selected, onPick, equippedOnly = false }:
  { sites: SiteSummary[]; selected?: string | null; onPick: (id: string) => void; equippedOnly?: boolean }) {
  const list = equippedOnly ? sites.filter((s) => s.equipped) : sites
  return (
    <div className="side-list">
      {list.map((s) => (
        <button key={s.id} className={`side-item${s.id === selected ? ' on' : ''}`} onClick={() => onPick(s.id)} title={s.mode_title}>
          <span className={`dot ${s.status}`} />
          <span>{s.title}</span>
        </button>
      ))}
    </div>
  )
}
