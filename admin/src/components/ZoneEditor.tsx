import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

type Pt = [number, number]
type Key = (string | number)[]
interface Zone { key: Key; points: Pt[] }
interface Target { key: Key; type: 'crosswalk' | 'wait' | 'approach'; label: string }
interface Cam { id: string; title: string; width: number; height: number; custom: boolean; zones: Zone[] }
interface ZonesData { targets: Target[]; cameras: Cam[] }

const COLORS: Record<Target['type'], string> = { crosswalk: '#f2c21b', wait: '#e070d0', approach: '#17b86c' }
const TYPE_TITLES: Record<Target['type'], string> = { crosswalk: 'Переход', wait: 'Зона ожидания', approach: 'Подход транспорта' }
const k = (key: Key) => key.join('|')

/** Редактор зон компьютерного зрения на кадре камеры. Координаты хранятся в долях кадра (0…1). */
export function ZoneEditor({ siteId }: { siteId: string }) {
  const [data, setData] = useState<ZonesData | null>(null)
  const [camIdx, setCamIdx] = useState(0)
  const [zones, setZones] = useState<Zone[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [stamp, setStamp] = useState(() => Date.now())
  const svg = useRef<SVGSVGElement>(null)
  const drag = useRef<{ zone: string; i: number } | null>(null)

  const load = useCallback((keepCam = true) => {
    api<ZonesData>(`/api/sites/${siteId}/zones`).then((d) => {
      setData(d)
      if (!keepCam) setCamIdx(0)
    })
  }, [siteId])
  useEffect(() => { load(false) }, [load])
  const cam = data?.cameras[camIdx]
  useEffect(() => {
    if (cam) {
      setZones(cam.zones.map((z) => ({ key: z.key, points: z.points.map((p) => [p[0], p[1]] as Pt) })))
      setDirty(false)
      setSel(null)
    }
  }, [cam])

  if (!data || !cam) return <div className="panel empty">Загружаю разметку…</div>
  const W = cam.width, H = cam.height
  const byKey = new Map(zones.map((z) => [k(z.key), z]))
  const targets = data.targets
  const selZone = sel ? byKey.get(sel) : undefined
  const selTarget = targets.find((t) => k(t.key) === sel)
  const handleR = 7 * W / (svg.current?.getBoundingClientRect().width || W)

  const toLocal = (e: { clientX: number; clientY: number }): Pt => {
    const r = svg.current!.getBoundingClientRect()
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]
  }
  const update = (key: string, fn: (pts: Pt[]) => Pt[]) => {
    setZones((zs) => zs.map((z) => (k(z.key) === key ? { ...z, points: fn(z.points) } : z)))
    setDirty(true)
    setMsg(null)
  }
  const onMove = (e: React.PointerEvent) => {
    if (!drag.current) return
    const p = toLocal(e)
    const { zone, i } = drag.current
    update(zone, (pts) => pts.map((q, j) => (j === i ? p : q)))
  }
  const insertPoint = (e: React.MouseEvent, zone: Zone) => {
    const p = toLocal(e)
    let best = 0, bestD = Infinity
    zone.points.forEach((a, i) => {
      const b = zone.points[(i + 1) % zone.points.length]
      const dx = b[0] - a[0], dy = b[1] - a[1]
      const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)))
      const d = Math.hypot(a[0] + dx * t - p[0], a[1] + dy * t - p[1])
      if (d < bestD) { bestD = d; best = i }
    })
    update(k(zone.key), (pts) => [...pts.slice(0, best + 1), p, ...pts.slice(best + 1)])
  }
  const addZone = (t: Target) => {
    setZones((zs) => [...zs, { key: t.key, points: [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]] }])
    setSel(k(t.key))
    setDirty(true)
  }
  const removeZone = (key: string) => {
    setZones((zs) => zs.filter((z) => k(z.key) !== key))
    setSel(null)
    setDirty(true)
  }
  const save = async () => {
    try {
      const d = await api<ZonesData>(`/api/sites/${siteId}/zones`, { method: 'PUT', body: JSON.stringify({ cameras: { [cam.id]: zones } }) })
      setData(d)
      setMsg({ ok: true, text: 'Сохранено: объект уже работает по новой разметке.' })
    } catch (e) {
      const detail = (e as { detail?: string[] }).detail
      setMsg({ ok: false, text: Array.isArray(detail) ? detail.join('; ') : 'Не удалось сохранить.' })
    }
  }
  const reset = async () => {
    const d = await api<ZonesData>(`/api/sites/${siteId}/zones/${cam.id}`, { method: 'DELETE' })
    setData(d)
    setMsg({ ok: true, text: 'Разметка камеры сброшена к автоматической.' })
  }

  return (
    <div className="zone-editor">
      <section className="panel tight">
        <div className="ze-bar">
          <div className="seg" role="group" aria-label="Камера">
            {data.cameras.map((c, i) => (
              <button key={c.id} className={i === camIdx ? 'on' : ''} onClick={() => (!dirty || confirmLeave()) && setCamIdx(i)}>
                Камера {i + 1}{c.custom ? ' · своя' : ''}
              </button>
            ))}
          </div>
          <span className="side-text">{cam.title}</span>
          <button className="btn" style={{ marginLeft: 'auto' }} onClick={() => setStamp(Date.now())}>Обновить кадр</button>
        </div>
        <div className="ze-canvas">
          <img src={`/video/${cam.id}.jpg?raw=1&t=${stamp}`} alt={`Кадр: ${cam.title}`} draggable={false} />
          <svg ref={svg} viewBox={`0 0 ${W} ${H}`} onPointerMove={onMove} onPointerUp={() => (drag.current = null)}
               onPointerLeave={() => (drag.current = null)} onClick={(e) => e.target === svg.current && setSel(null)}>
            {zones.map((z) => {
              const t = targets.find((x) => k(x.key) === k(z.key))
              const on = k(z.key) === sel
              const color = COLORS[t?.type || 'wait']
              const pts = z.points.map(([x, y]) => `${x * W},${y * H}`).join(' ')
              return (
                <g key={k(z.key)}>
                  <polygon points={pts} fill={color} fillOpacity={on ? 0.25 : 0.08} stroke={color} strokeWidth={on ? 3 : 1.6}
                           vectorEffect="non-scaling-stroke" style={{ cursor: 'pointer' }}
                           onClick={(e) => { e.stopPropagation(); setSel(k(z.key)) }}
                           onDoubleClick={(e) => { e.stopPropagation(); if (on) insertPoint(e, z) }} />
                </g>
              )
            })}
            {selZone && selZone.points.map(([x, y], i) => (
              <circle key={i} cx={x * W} cy={y * H} r={handleR} className="ze-handle" fill={COLORS[selTarget?.type || 'wait']}
                      onPointerDown={(e) => { drag.current = { zone: sel!, i }; try { (e.target as Element).setPointerCapture(e.pointerId) } catch { /* захват недоступен */ } }}
                      onContextMenu={(e) => { e.preventDefault(); if (selZone.points.length > 3) update(sel!, (pts) => pts.filter((_, j) => j !== i)) }} />
            ))}
          </svg>
        </div>
        <p className="note">
          Выберите зону в списке или на кадре. Точки перетаскиваются мышью; двойной щелчок по зоне добавляет точку,
          правый щелчок по точке удаляет её. Нижняя середина рамки человека или машины должна попадать в зону.
        </p>
        <div className="save-bar">
          <span className={`msg ${msg?.ok ? 'ok' : 'bad'}`}>{msg?.text || (dirty ? 'Есть несохранённые изменения.' : cam.custom ? 'Разметка задана вручную.' : 'Автоматическая разметка по калибровке камеры.')}</span>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn" disabled={!cam.custom} onClick={reset}>Сбросить к автоматической</button>
            <button className="btn" disabled={!dirty} onClick={() => { setZones(cam.zones); setDirty(false) }}>Отменить</button>
            <button className="btn primary" disabled={!dirty} onClick={save}>Сохранить</button>
          </div>
        </div>
      </section>

      <section className="panel tight">
        <div className="panel-title caps">Зоны камеры</div>
        {(['crosswalk', 'wait', 'approach'] as const).map((type) => (
          <div key={type} className="ze-group">
            <div className="ze-type" style={{ color: COLORS[type] }}>{TYPE_TITLES[type]}</div>
            {targets.filter((t) => t.type === type).map((t) => {
              const has = byKey.has(k(t.key))
              return (
                <div key={k(t.key)} className={`ze-item${k(t.key) === sel ? ' on' : ''}`}>
                  <button className="side-link" disabled={!has} onClick={() => setSel(k(t.key))}>
                    {t.label.split(' · ').slice(1).join(' · ')}
                  </button>
                  {has
                    ? <button className="btn" onClick={() => removeZone(k(t.key))} title="Камера не видит эту зону">Убрать</button>
                    : <button className="btn" onClick={() => addZone(t)}>Добавить</button>}
                </div>
              )
            })}
          </div>
        ))}
      </section>
    </div>
  )
}

function confirmLeave(): boolean {
  // в артефактах и встроенных окнах confirm() может быть недоступен — тогда просто переключаемся
  try { return window.confirm('Несохранённые изменения зон будут потеряны. Переключить камеру?') } catch { return true }
}
