import { useEffect, useRef, useState } from 'react'

export type Kind = 'crossing' | 'tee' | 'cross'
export type Status = 'ok' | 'warn' | 'alarm'

export interface SiteSummary {
  id: string
  kind: Kind
  title: string
  lat: number
  lon: number
  streets: string[]
  status: Status
  mode: string
  mode_title: string
  source: string
  stage: string
  waiting: number
  flow_vph: number
  cameras_ok: number
  cameras_total: number
}

export interface SiteEvent {
  ts: string
  level: 'info' | 'warn' | 'critical'
  kind: string
  message: string
  site?: string
}

export interface Snapshot extends SiteSummary {
  ts: number
  mode_reason: string
  stage_id: string | null
  next_stage: string | null
  stage_time_s: number
  status_text: string
  info: Record<string, number | boolean | string[] | null>
  signals: Record<string, string>
  observation: {
    waiting: Record<string, number | null>
    max_wait: Record<string, number | null>
    on_crosswalk: Record<string, number | null>
    queue: Record<string, number | null>
    eta: Record<string, number | null>
    flow_vph: Record<string, number | null>
    flow_window_s: Record<string, number>
    emergency: string[]
  }
  cameras: { id: string; title: string; ok: boolean; covers: string[] }[]
  trip: string | null
  forced: string | null
  events: SiteEvent[]
}

export interface Group {
  id: string
  kind: 'veh' | 'ped'
  title: string
  length_m: number
}

export interface SiteInfo {
  id: string
  kind: Kind
  title: string
  lat: number
  lon: number
  main_street: string
  streets: string[]
  road_bearing_deg: number
  addresses: { address: string; distance_m: number }[]
  layout: {
    kind: Kind
    groups: Group[]
    stages: { id: string; title: string; veh: string[]; ped: string[] }[]
    conflicts: string[][]
  }
  params: Record<string, number>
}

export interface SearchHit {
  type: 'site' | 'address'
  site_id: string
  label: string
  sublabel: string
}

export interface StatsPoint {
  t: string
  ped_served: number
  wait_avg: number | null
  wait_max: number
  violations: number
  groups: number
  ped_phases: number
  veh_passed: number
  veh_flow_vph: number
  stop_share: number | null
  demo: boolean
}

export interface StatsSummary {
  ped_served: number
  wait_avg: number | null
  wait_max: number
  violation_share: number | null
  groups: number
  ped_phases: number
  veh_passed: number
  stop_share: number | null
  mode_share: Record<string, number>
  demo_share: number
}

export interface Stats {
  since: string
  bucket_min: number
  summary: StatsSummary
  series: StatsPoint[]
  wait_hist: { from_s: number; count: number }[]
}

export const KIND_TITLES: Record<Kind, string> = {
  crossing: 'Пешеходный переход',
  tee: 'Т-образный перекрёсток',
  cross: 'Перекрёсток',
}

export const STATUS_TITLES: Record<Status, string> = { ok: 'Норма', warn: 'Внимание', alarm: 'Авария' }

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!r.ok) {
    let detail: unknown = r.statusText
    try {
      detail = (await r.json()).detail
    } catch {
      /* тело без JSON */
    }
    throw Object.assign(new Error(`Ошибка ${r.status}`), { detail })
  }
  return r.json() as Promise<T>
}

export const post = <T,>(path: string, body: unknown = {}) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body) })

/** Живые данные по WebSocket с переподключением. */
export function useLive<T>(path: string | null): { data: T | null; connected: boolean } {
  const [data, setData] = useState<T | null>(null)
  const [connected, setConnected] = useState(false)
  const retry = useRef(0)
  useEffect(() => {
    if (!path) return
    let ws: WebSocket | null = null
    let closed = false
    let timer: number | undefined
    const open = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}${path}`)
      ws.onopen = () => {
        retry.current = 0
        setConnected(true)
      }
      ws.onmessage = (e) => setData(JSON.parse(e.data))
      ws.onclose = () => {
        setConnected(false)
        if (!closed) timer = window.setTimeout(open, Math.min(5000, 500 * 2 ** retry.current++))
      }
    }
    open()
    return () => {
      closed = true
      window.clearTimeout(timer)
      ws?.close()
    }
  }, [path])
  return { data, connected }
}

export const fmt = (v: number | null | undefined, digits = 0, unit = '') =>
  v == null || Number.isNaN(v) ? '—' : `${v.toLocaleString('ru-RU', { maximumFractionDigits: digits, minimumFractionDigits: digits })}${unit ? ' ' + unit : ''}`

export const pct = (v: number | null | undefined, digits = 0) => (v == null ? '—' : fmt(v * 100, digits, '%'))

export const timeOf = (iso: string) => iso.slice(11, 19)
