import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  api, fmt, KIND_TITLES, lastSite, post, rememberSite, timeOf, useLive, useOverview,
  type Incident, type SiteInfo, type Snapshot,
} from '../api'
import { CameraPtz } from '../components/CameraPtz'
import { CameraView } from '../components/CameraView'
import { StaticSwitch } from '../components/StaticSwitch'
import { Schematic } from '../components/Schematic'
import { MobileSitePicker } from '../components/MobileSitePicker'
import { Shell } from '../components/Shell'
import { SiteList } from '../components/SiteList'

const SIGNAL_TITLES: Record<string, string> = {
  red: 'красный', red_yellow: 'красный с жёлтым', green: 'зелёный', green_blink: 'зелёный мигает',
  yellow: 'жёлтый', yellow_flash: 'жёлтый мигающий', off: 'выключен',
}
const DEVICE_MODES: Record<string, string> = { remote: 'управляет система', local: 'своя программа', flash: 'жёлтый мигающий' }
const MODES: [string | null, string, string][] = [
  [null, 'Умный', 'Система решает по камерам'],
  ['fixed', 'Фикс. цикл системы', 'Система ведёт фиксированный цикл, без камер'],
  ['flashing', 'Аварийный', 'Жёлтый мигающий'],
]

function Lamp({ s }: { s?: string }) {
  const v = s || 'red'
  const color = v === 'red' ? 'red' : v.startsWith('green') ? 'green' : v === 'off' ? '' : 'yellow'
  return <i className={`lamp ${color}${v === 'green_blink' || v === 'yellow_flash' ? ' blink' : ''}`} title={SIGNAL_TITLES[v] || v} />
}

function Alerts({ items }: { items: Incident[] }) {
  if (items.length === 0) return <div className="alerts quiet">Сбоев и аварий нет.</div>
  return (
    <div className="alerts" role="log">
      {items.map((e, i) => {
        const fixed = e.level === 'info' || /(^|[^е])исправна|восстановлен|принято/.test(e.message)
        return (
          <div className="a" key={i}>
            <span aria-hidden>{fixed ? '✓' : '⚠'}</span>
            <time>{timeOf(e.ts).slice(0, 5)}</time>
            <span>{e.message}<small>{e.site_title}</small></span>
          </div>
        )
      })}
    </div>
  )
}

export function MonitoringPage() {
  const { id } = useParams()
  const nav = useNavigate()
  const { sites, connected } = useOverview()
  const [info, setInfo] = useState<SiteInfo | null>(null)
  const [alerts, setAlerts] = useState<Incident[]>([])
  const [busy, setBusy] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [calib, setCalib] = useState(false)

  // выбранный объект: из адреса, иначе последний открытый, иначе первый оснащённый
  useEffect(() => {
    if (id || sites.length === 0) return
    const last = lastSite()
    const pick = sites.find((s) => s.id === last) || sites.find((s) => s.equipped) || sites[0]
    nav(`/monitoring/${pick.id}`, { replace: true })
  }, [id, sites, nav])
  useEffect(() => {
    if (!id) return
    rememberSite(id)
    setLoadError('')
    // прежний объект остаётся на экране, пока грузится новый; опоздавший ответ отбрасывается
    let actual = true
    api<SiteInfo>(`/api/sites/${id}`)
      .then((d) => { if (actual) setInfo(d) })
      .catch(() => { if (actual) setLoadError('Не удалось загрузить объект: нет связи с центром.') })
    return () => { actual = false }
  }, [id])
  useEffect(() => {
    const load = () => api<Incident[]>('/api/events?limit=8').then(setAlerts).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [])
  const live = useLive<Snapshot>(info?.equipped ? `/ws/sites/${info.id}` : null)
  const snap = live.data && live.data.id === info?.id ? live.data : null

  const act = async (path: string, body: unknown = {}) => {
    if (!info) return
    setBusy(true)
    try { await post(`/api/sites/${info.id}${path}`, body) } finally { setBusy(false) }
  }
  const near = info?.addresses[0]?.address
  const groups = info?.layout?.groups || []
  const o = snap?.observation

  return (
    <Shell connected={connected}>
      <div className="page with-left">
        <aside className="sidebar side-after">
          <div className="desktop-only">
            <Link to={`/map${id ? `?site=${id}` : ''}`} className="side-link">Выбрать камеру на карте</Link>
            <div className="side-link" style={{ cursor: 'default' }}>Список камер</div>
            <SiteList sites={sites} selected={id} onPick={(sid) => nav(`/monitoring/${sid}`)} />
          </div>

          <div className="side-h">Режим работы светофора</div>
          {!info ? <div className="side-text">…</div> : !info.equipped ? (
            <div className="side-text">Не оснащён: контроллер работает по своей фиксированной программе.</div>
          ) : (
            <>
              <div className="now-stage">{snap ? snap.mode_title : '…'}</div>
              <div className="side-text">{snap?.engaged ? (snap.next_stage ? `→ ${snap.next_stage}` : snap.stage) : ''}</div>
              <div className="side-text">{snap?.status_text}</div>
              {snap?.trip && (
                <div className="trip">
                  <span>Сработала защита: {snap.trip}</span>
                  <button className="btn" disabled={busy} onClick={() => act('/reset')}>Сбросить аварию</button>
                </div>
              )}
              <StaticSwitch siteId={info.id} value={snap ? (snap.static ?? null) : undefined} reason={snap?.static_reason} />
              <div className="seg" role="group" aria-label="Режим оператора" style={{ marginTop: 4 }}>
                {MODES.map(([m, t, hint]) => (
                  <button key={t} title={hint} className={(snap?.forced ?? null) === m ? 'on' : ''} disabled={busy}
                          onClick={() => act('/mode', { mode: m })}>{t}</button>
                ))}
              </div>
            </>
          )}

          <div className="side-h">Журнал событий</div>
          {info?.equipped ? (
            (snap?.events || []).length === 0 ? <div className="side-text">Событий пока нет.</div> :
              <div>{snap!.events.slice(0, 12).map((e, k) => (
                <div key={k} className={`event-row ${e.level}`}><time>{timeOf(e.ts).slice(0, 5)}</time><span>{e.message}</span></div>
              ))}</div>
          ) : <div className="side-text">—</div>}
        </aside>

        <div className="stack">
          <MobileSitePicker sites={sites} selected={id} onPick={(sid) => sid && nav(`/monitoring/${sid}`)} />
          <section className="panel">
            {info && (
              <div className="panel-title" style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' }}>
                <span>{info.title}</span>
                <span className="muted" style={{ fontSize: 12 }}>{KIND_TITLES[info.kind]} · {info.streets.join(', ')}</span>
                {snap && <span className={`chip ${snap.status}`} style={{ marginLeft: 'auto' }}><i />{snap.mode_title}</span>}
                {info.equipped && (
                  <button className={`btn${calib ? ' on' : ''}`} aria-pressed={calib} onClick={() => setCalib((v) => !v)}
                          title="Повернуть камеры: ход ±90° от положения при монтаже">
                    {calib ? 'Готово' : 'Повернуть камеры'}
                  </button>
                )}
              </div>
            )}
            {info?.equipped ? (
              <div className="mon-grid">
                {info.cameras.map((c, k) => {
                  const st = snap?.cameras.find((x) => x.id === c.id)
                  return (
                    <figure key={c.id} className={`cam${st && st.level === 'fault' ? ' bad' : ''}${st && st.level === 'warn' ? ' warn' : ''}`}>
                      <div className="cap">
                        <span>Камера {k + 1}{near ? ` · ${near}` : ''}</span>
                        <span className={st && !st.ok ? `cam-state ${st.level}` : 'muted'}>
                          {st ? (st.ok ? `${fmt(st.fps, 1)} к/с` : st.fault_title) : ''}
                        </span>
                      </div>
                      <CameraView key={c.id} camId={c.id} alt={c.title} />
                      {calib && info && <CameraPtz siteId={info.id} camId={c.id} ptz={st?.ptz} />}
                    </figure>
                  )
                })}
              </div>
            ) : info ? (
              <div className="side-text" style={{ maxWidth: '68ch' }}>
                Камер на объекте нет. Чтобы включить его в систему, нужно установить две широкоугольные камеры
                и подключить дорожный контроллер к центру. Рядом: {info.addresses.map((a) => a.address).slice(0, 3).join('; ')}.
              </div>
            ) : <div className="empty">{loadError || 'Загружаю объект…'}</div>}
          </section>

          <section className="panel">
            <div className="mon-grid">
              <div>
                <div className="panel-title">Схема объекта</div>
                {info?.layout ? <Schematic info={info} snap={snap} /> : <div className="empty">Схема есть только у оснащённых объектов.</div>}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
                <div>
                  <div className="panel-title caps">Оповещения</div>
                  <Alerts items={alerts} />
                </div>
                {info?.equipped && (
                  <div>
                    <div className="panel-title caps">Обстановка</div>
                    <div className="tw">
                      <table className="t">
                        <thead><tr><th>Группа</th><th>Сигнал</th><th>Ждут / очередь</th><th>Поток</th></tr></thead>
                        <tbody>
                          {groups.map((g) => (
                            <tr key={g.id}>
                              <td>{g.title}{o?.emergency.includes(g.id) && <span className="chip alarm" style={{ marginLeft: 6 }}>спецтранспорт</span>}</td>
                              <td><Lamp s={snap?.signals[g.id]} /></td>
                              <td>{g.kind === 'ped'
                                ? (o?.waiting[g.id] == null ? 'не видно' : `${fmt(o.waiting[g.id])}${o.waiting[g.id] ? ` · ${fmt(o.max_wait[g.id])} с` : ''}`)
                                : (o?.queue[g.id] == null ? 'не видно' : fmt(o.queue[g.id]))}</td>
                              <td>{g.kind === 'veh' ? fmt(o?.flow_vph[g.id], 0, 'авт/ч') : ''}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <dl className="kv" style={{ marginTop: 10 }}>
                      <dt>Контроллер объекта</dt>
                      <dd style={{ color: snap?.equipment.connected ? 'var(--green)' : 'var(--alarm)' }}>
                        {snap ? (snap.equipment.connected ? DEVICE_MODES[snap.equipment.mode || ''] || snap.equipment.mode : 'нет связи') : '…'}
                      </dd>
                    </dl>
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>
      </div>
    </Shell>
  )
}
