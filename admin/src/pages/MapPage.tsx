import L from 'leaflet'
import { useEffect, useMemo, useRef, useState } from 'react'
import { MapContainer, Marker, Popup, TileLayer, useMap } from 'react-leaflet'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, fmt, KIND_TITLES, STATUS_TITLES, timeOf, useLive, type SearchHit, type SiteEvent, type SiteSummary } from '../api'
import { Glyph, glyphSvg } from '../components/Glyph'
import { SearchBox } from '../components/SearchBox'

const CENTER: [number, number] = [55.6765, 37.7535]

function icon(s: SiteSummary, selected: boolean) {
  return L.divIcon({
    className: '',
    html: `<div class="marker ${s.status}${selected ? ' sel' : ''}">${glyphSvg(s.kind)}</div>`,
    iconSize: [38, 38],
    iconAnchor: [19, 19],
    popupAnchor: [0, -18],
  })
}

function FitAll({ sites }: { sites: SiteSummary[] }) {
  const map = useMap()
  const done = useRef(false)
  useEffect(() => {
    if (done.current || sites.length === 0) return
    done.current = true
    map.fitBounds(L.latLngBounds(sites.map((s) => [s.lat, s.lon] as [number, number])).pad(0.15))
  }, [sites, map])
  return null
}

function FlyTo({ target }: { target: [number, number] | null }) {
  const map = useMap()
  useEffect(() => {
    if (target) map.flyTo(target, Math.max(map.getZoom(), 17), { duration: 0.6 })
  }, [target, map])
  return null
}

export function MapPage() {
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const selected = params.get('site')
  const [initial, setInitial] = useState<SiteSummary[]>([])
  const [district, setDistrict] = useState('')
  const [events, setEvents] = useState<SiteEvent[]>([])
  const [fly, setFly] = useState<[number, number] | null>(null)
  const live = useLive<SiteSummary[]>('/ws/overview')

  useEffect(() => {
    api<{ district: string; sites: SiteSummary[] }>('/api/overview').then((d) => {
      setInitial(d.sites)
      setDistrict(d.district)
    })
    const load = () => api<SiteEvent[]>('/api/events?limit=12').then(setEvents).catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  const sites = useMemo(() => {
    const byId = new Map((live.data || []).map((s) => [s.id, s]))
    return initial.map((s) => ({ ...s, ...(byId.get(s.id) || {}) }))
  }, [initial, live.data])
  const names = useMemo(() => new Map(sites.map((s) => [s.id, s.title])), [sites])
  const counts = { ok: 0, warn: 0, alarm: 0 }
  sites.forEach((s) => counts[s.status]++)

  const select = (id: string) => {
    setParams({ site: id }, { replace: true })
    const s = sites.find((x) => x.id === id)
    if (s) setFly([s.lat, s.lon])
  }
  const onPick = (h: SearchHit) => select(h.site_id)

  const groups: [string, SiteSummary[]][] = [
    ['Пешеходные переходы', sites.filter((s) => s.kind === 'crossing')],
    ['Перекрёстки', sites.filter((s) => s.kind !== 'crossing')],
  ]

  return (
    <div className="map-page">
      <aside className="side">
        <div className="side-head">
          <div className="brand">
            <img src="/favicon.svg" width={26} height={26} alt="" />
            <div>
              <h1>Умные переходы</h1>
              <small>{district || 'Люблино, Москва'} · {sites.length} объектов</small>
            </div>
          </div>
          <SearchBox onPick={onPick} />
          <div className="counts">
            <span className="chip ok"><i />{STATUS_TITLES.ok}: {counts.ok}</span>
            <span className="chip warn"><i />{STATUS_TITLES.warn}: {counts.warn}</span>
            <span className="chip alarm"><i />{STATUS_TITLES.alarm}: {counts.alarm}</span>
            {!live.connected && <span className="chip">нет связи с центром</span>}
          </div>
        </div>
        <div className="side-list">
          {groups.map(([title, list]) => (
            <div key={title}>
              <h4>{title}</h4>
              {list.map((s) => (
                <button key={s.id} className={`site-row${s.id === selected ? ' sel' : ''}`} onClick={() => select(s.id)}
                        onDoubleClick={() => nav(`/sites/${s.id}`)}>
                  <Glyph kind={s.kind} status={s.status} />
                  <span>
                    <span className="t">{s.title}</span>
                    <span className="s">{s.status === 'ok' ? s.stage : s.mode_title}</span>
                  </span>
                  <span className="n">
                    <span className="mono">{fmt(s.waiting)} ждут</span>
                    <span className="mono">{fmt(s.flow_vph)} авт/ч</span>
                  </span>
                </button>
              ))}
            </div>
          ))}
          <h4>Важные события</h4>
          {events.length === 0 && <div className="empty" style={{ padding: '4px 8px' }}>Отказов и аварий нет.</div>}
          {events.map((e, i) => (
            <div key={i} className={`event-row ${e.level}`}>
              <time>{timeOf(e.ts)}</time>
              <span>
                <b style={{ fontWeight: 500 }}>{names.get(e.site || '') || e.site}</b>. {e.message}
              </span>
            </div>
          ))}
        </div>
      </aside>
      <div className="map-wrap">
        <MapContainer center={CENTER} zoom={16} zoomControl>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            maxZoom={19}
          />
          <FitAll sites={initial} />
          <FlyTo target={fly} />
          {sites.map((s) => (
            <Marker key={s.id} position={[s.lat, s.lon]} icon={icon(s, s.id === selected)}
                    eventHandlers={{ click: () => setParams({ site: s.id }, { replace: true }) }}>
              <Popup>
                <div className="pop">
                  <h3>{s.title}</h3>
                  <div className="muted">{KIND_TITLES[s.kind]} · {s.streets.join(', ')}</div>
                  <dl className="kv">
                    <dt>Состояние</dt><dd>{STATUS_TITLES[s.status]}</dd>
                    <dt>Режим</dt><dd>{s.mode_title}</dd>
                    <dt>Сейчас</dt><dd>{s.stage}</dd>
                    <dt>Ждут пешеходов</dt><dd>{fmt(s.waiting)}</dd>
                    <dt>Интенсивность</dt><dd>{fmt(s.flow_vph, 0, 'авт/ч')}</dd>
                    <dt>Камеры</dt><dd>{s.cameras_ok} из {s.cameras_total}</dd>
                  </dl>
                  <button className="btn primary" onClick={() => nav(`/sites/${s.id}`)}>Открыть объект</button>
                </div>
              </Popup>
            </Marker>
          ))}
        </MapContainer>
        <div className="legend">
          <span><i className="lamp green" /> работает в адаптивном режиме</span>
          <span><i className="lamp yellow" /> отказ камеры, резервный режим</span>
          <span><i className="lamp red" /> авария, жёлтый мигающий</span>
        </div>
      </div>
    </div>
  )
}
