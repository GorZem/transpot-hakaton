import { useEffect, useState } from 'react'
import { CircleMarker, Pane, Polyline, Tooltip } from 'react-leaflet'
import { api, fmt } from '../api'

export interface Arm {
  street: string
  group: string
  line: [number, number][]
  lamp: [number, number]
  stopped: number | null
  moving: number | null
  level: 'free' | 'moderate' | 'heavy' | 'jam' | 'unknown'
  signal: string | null
}
export interface SiteArms { id: string; arms: Arm[] }

export const LOAD: Record<Arm['level'], { color: string; title: string }> = {
  free: { color: '#17b86c', title: 'свободно' },
  moderate: { color: '#f2c21b', title: 'средне' },
  heavy: { color: '#ff8a1c', title: 'плотно' },
  jam: { color: '#e0474f', title: 'затор' },
  unknown: { color: '#8a857a', title: 'не видно камерами' },
}

const LAMP: Record<string, { color: string; title: string }> = {
  green: { color: '#1fc46a', title: 'зелёный' },
  green_blink: { color: '#1fc46a', title: 'зелёный мигает' },
  yellow: { color: '#f4b400', title: 'жёлтый' },
  red_yellow: { color: '#f4b400', title: 'красный с жёлтым' },
  yellow_flash: { color: '#f4b400', title: 'жёлтый мигающий' },
  red: { color: '#e5383b', title: 'красный' },
}

/** Подходы оснащённых объектов: опрос центра, пока включён хотя бы один слой. */
export function useArms(on: boolean): SiteArms[] {
  const [data, setData] = useState<SiteArms[]>([])
  useEffect(() => {
    if (!on) return
    let alive = true
    const load = () => api<SiteArms[]>('/api/map/arms').then((d) => { if (alive) setData(d) }).catch(() => {})
    load()
    const t = setInterval(load, 1500)
    return () => { alive = false; clearInterval(t) }
  }, [on])
  return on ? data : []
}

/** Загруженность: толстая линия вдоль каждого ответвления, цвет — по числу стоящих машин. */
export function LoadLayer({ sites }: { sites: SiteArms[] }) {
  return (
    <Pane name="arm-load" style={{ zIndex: 410 }}>
      {sites.flatMap((s) => s.arms.map((a, i) => {
        const l = LOAD[a.level]
        return (
          <Polyline key={`${s.id}-${i}`} positions={a.line} pathOptions={{
            color: l.color, weight: 11, opacity: 0.8, lineCap: 'round', dashArray: a.level === 'unknown' ? '2 14' : undefined,
          }}>
            <Tooltip sticky>
              {a.street}: {l.title}{a.stopped != null ? ` · стоят ${fmt(a.stopped)}, подъезжают ${fmt(a.moving)}` : ''}
            </Tooltip>
          </Polyline>
        )
      }))}
    </Pane>
  )
}

/** Сигнал для транспорта: «лампа» у стоп-линии каждого подхода. */
export function SignalLayer({ sites }: { sites: SiteArms[] }) {
  return (
    <Pane name="arm-signal" style={{ zIndex: 620 }}>
      {sites.flatMap((s) => s.arms.map((a, i) => {
        const l = (a.signal && LAMP[a.signal]) || { color: '#6f6a60', title: 'нет данных' }
        const blink = a.signal === 'green_blink' || a.signal === 'yellow_flash'
        return (
          <CircleMarker key={`${s.id}-${i}-${blink}`} center={a.lamp} radius={7} className={blink ? 'lamp-blink' : ''}
                        pathOptions={{ color: '#111', weight: 2.5, fillColor: l.color, fillOpacity: 1 }}>
            <Tooltip>{a.street}: {l.title}</Tooltip>
          </CircleMarker>
        )
      }))}
    </Pane>
  )
}
