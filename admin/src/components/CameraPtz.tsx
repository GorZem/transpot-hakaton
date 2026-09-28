import { useState } from 'react'
import { api, post, type Ptz } from '../api'

const STEPS = [-15, -5, 5, 15]

/**
 * Поворот камеры из админки. Ход — ±90° по азимуту от положения при монтаже.
 * Пока камера поворачивается, объект работает по фиксированному плану; после остановки
 * центр сам пересчитывает калибровку и зоны распознавания.
 */
export function CameraPtz({ siteId, camId, ptz, onChange }: {
  siteId: string; camId: string; ptz: Ptz | null | undefined; onChange?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [drag, setDrag] = useState<number | null>(null)
  if (!ptz) return <div className="ptz muted">Поворот: нет связи с поворотным устройством</div>
  const lim = ptz.limits?.pan_deg || [-90, 90]
  const goal = ptz.goal?.pan_deg ?? ptz.pan_deg
  const send = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setErr('')
    try { await fn(); onChange?.() } catch (e) {
      const d = (e as { detail?: unknown }).detail
      setErr(typeof d === 'string' ? d : 'Камера не выполнила команду')
    } finally { setBusy(false) }
  }
  const pan = (deg: number) => send(() => api(`/api/sites/${siteId}/cameras/${camId}/ptz`,
    { method: 'PUT', body: JSON.stringify({ pan_deg: deg }) }))
  const tilt = (d: number) => send(() => api(`/api/sites/${siteId}/cameras/${camId}/ptz`,
    { method: 'PUT', body: JSON.stringify({ tilt_deg: d, relative: true }) }))
  const sign = (v: number) => `${v > 0 ? '+' : ''}${Math.round(v)}°`
  return (
    <div className={`ptz${ptz.moving ? ' moving' : ''}`}>
      <div className="ptz-row">
        {STEPS.map((d) => (
          <button key={d} className="btn" disabled={busy || goal + d < lim[0] - 0.01 || goal + d > lim[1] + 0.01}
                  title={`${d < 0 ? 'Влево' : 'Вправо'} на ${Math.abs(d)}°`} onClick={() => pan(Math.max(lim[0], Math.min(lim[1], goal + d)))}>
            {d < 0 ? `◀ ${-d}°` : `${d}° ▶`}
          </button>
        ))}
        <button className="btn" disabled={busy} title="Выше на 3°" onClick={() => tilt(-3)}>▲</button>
        <button className="btn" disabled={busy} title="Ниже на 3°" onClick={() => tilt(3)}>▼</button>
        <button className="btn" disabled={busy || (ptz.pan_deg === 0 && ptz.tilt_deg === 0)}
                onClick={() => send(() => post(`/api/sites/${siteId}/cameras/${camId}/ptz/home`))}>Как при монтаже</button>
      </div>
      <label className="ptz-slider">
        <span>{sign(lim[0])}</span>
        <input type="range" min={lim[0]} max={lim[1]} step={1} value={Math.round(drag ?? goal)} disabled={busy}
               aria-label="Поворот по азимуту от положения при монтаже"
               onChange={(e) => setDrag(+e.target.value)}
               onPointerUp={() => { if (drag !== null) { pan(drag); setDrag(null) } }}
               onKeyUp={() => { if (drag !== null) { pan(drag); setDrag(null) } }} />
        <span>{sign(lim[1])}</span>
        {drag !== null && <b>{sign(drag)}</b>}
      </label>
      <div className="ptz-info">
        <span>Поворот {sign(ptz.pan_deg)} · наклон {sign(ptz.tilt_deg)} от монтажного · азимут {Math.round(ptz.azimuth_deg)}°</span>
        <span className="ptz-state">{ptz.moving ? 'поворачивается — фиксированный план, зоны будут пересчитаны' : ''}</span>
        {err && <span className="ptz-err">{err}</span>}
      </div>
    </div>
  )
}
