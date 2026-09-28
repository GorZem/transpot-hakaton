import { Link } from 'react-router-dom'
import { STATUS_TITLES, type SiteSummary } from '../api'

/** Выбор объекта на телефоне: компактный список вместо боковой панели из 14 строк. */
export function MobileSitePicker({ sites, selected, onPick, allLabel, mapLink = true, equippedOnly = false }: {
  sites: SiteSummary[]
  selected?: string | null
  onPick: (id: string | null) => void
  allLabel?: string          // если задано — первым пунктом «весь участок»
  mapLink?: boolean
  equippedOnly?: boolean
}) {
  const list = equippedOnly ? sites.filter((s) => s.equipped) : sites
  return (
    <div className="mobile-only m-picker">
      <label htmlFor="m-site" className="side-h" style={{ margin: 0 }}>Объект</label>
      <div className="m-picker-row">
        <select id="m-site" value={selected ?? ''} onChange={(e) => onPick(e.target.value || null)}>
          {allLabel && <option value="">{allLabel}</option>}
          {list.map((s) => (
            <option key={s.id} value={s.id}>{s.title} — {STATUS_TITLES[s.status]}</option>
          ))}
        </select>
        {mapLink && <Link to={`/map${selected ? `?site=${selected}` : ''}`} className="btn">Карта</Link>}
      </div>
    </div>
  )
}
