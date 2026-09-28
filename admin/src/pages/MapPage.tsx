import L from 'leaflet'
import { useEffect, useRef, useState } from 'react'
import { MapContainer, Marker, Popup, TileLayer, useMap } from 'react-leaflet'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { fmt, KIND_TITLES, STATUS_TITLES, useOverview, type SearchHit, type SiteSummary } from '../api'
import { glyphSvg } from '../components/Glyph'
import { SearchBox } from '../components/SearchBox'
import { Shell } from '../components/Shell'
import { SiteList } from '../components/SiteList'

function icon(s: SiteSummary, selected: boolean) {
  return L.divIcon({
    className: '',
    html: `<div class="marker ${s.status}${selected ? ' sel' : ''}">${glyphSvg(s.kind, '#1b1b1b')}</div>`,
    iconSize: [36, 36], iconAnchor: [18, 18], popupAnchor: [0, -18],
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
  const { sites, connected } = useOverview()
  const [fly, setFly] = useState<[number, number] | null>(null)

  const select = (id: string) => {
    setParams({ site: id }, { replace: true })
    const s = sites.find((x) => x.id === id)
    if (s) setFly([s.lat, s.lon])
  }

  return (
    <Shell connected={connected}>
      <div className="page with-left-wide">
        <aside className="sidebar side-after">
          <div className="side-h">Выбрать объект</div>
          <SearchBox onPick={(h: SearchHit) => select(h.site_id)} />
          <div className="side-text">Поиск по пересечению улиц или адресу дома. Двойной щелчок по объекту открывает мониторинг.</div>
          <div className="side-h">Оснащённые</div>
          <div onDoubleClick={() => selected && nav(`/monitoring/${selected}`)}>
            <SiteList sites={sites.filter((s) => s.equipped)} selected={selected} onPick={select} />
          </div>
          {sites.some((s) => !s.equipped) && <>
            <div className="side-h">Не оснащены</div>
            <SiteList sites={sites.filter((s) => !s.equipped)} selected={selected} onPick={select} />
          </>}
        </aside>
        <div className="map-panel">
          <MapContainer center={[55.6765, 37.7535]} zoom={16}>
            <TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                       url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" maxZoom={19} />
            <FitAll sites={sites} />
            <FlyTo target={fly} />
            {sites.map((s) => (
              <Marker key={s.id} position={[s.lat, s.lon]} icon={icon(s, s.id === selected)}
                      eventHandlers={{ click: () => setParams({ site: s.id }, { replace: true }) }}>
                <Popup>
                  <div className="pop">
                    <h3>{s.title}</h3>
                    <div className="muted">{KIND_TITLES[s.kind]} · {s.streets.join(', ')}</div>
                    {s.equipped ? (
                      <dl className="kv">
                        <dt>Состояние</dt><dd>{STATUS_TITLES[s.status]}</dd>
                        <dt>Режим</dt><dd>{s.mode_title}</dd>
                        <dt>Ждут пешеходов</dt><dd>{fmt(s.waiting)}</dd>
                        <dt>Поток</dt><dd>{fmt(s.flow_vph, 0, 'авт/ч')}</dd>
                        <dt>Камеры</dt><dd>{s.cameras_ok} из {s.cameras_total}</dd>
                      </dl>
                    ) : <p className="muted">Камер и связи с системой нет: работает по своей программе.</p>}
                    <button className="btn primary" onClick={() => nav(`/monitoring/${s.id}`)}>Открыть мониторинг</button>
                  </div>
                </Popup>
              </Marker>
            ))}
          </MapContainer>
          <div className="legend">
            <span><i className="lamp green" /> адаптивный режим</span>
            <span><i className="lamp yellow" /> отказ камеры или подхват</span>
            <span><i className="lamp red" /> авария, нет связи</span>
            <span><i className="lamp grey" /> не оснащён</span>
          </div>
        </div>
      </div>
    </Shell>
  )
}
