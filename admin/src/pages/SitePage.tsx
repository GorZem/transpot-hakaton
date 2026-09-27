import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, KIND_TITLES, STATUS_TITLES, useLive, type SiteInfo, type Snapshot } from '../api'
import { Glyph } from '../components/Glyph'
import { Monitoring } from '../components/Monitoring'
import { SettingsTab } from '../components/SettingsTab'
import { StatsTab } from '../components/StatsTab'

type Tab = 'monitor' | 'stats' | 'settings'
const TABS: [Tab, string][] = [['monitor', 'Мониторинг'], ['stats', 'Статистика'], ['settings', 'Настройки']]

export function SitePage() {
  const { id = '' } = useParams()
  const [info, setInfo] = useState<SiteInfo | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<Tab>(() => (location.hash.slice(1) as Tab) || 'monitor')
  const live = useLive<Snapshot>(info?.equipped ? `/ws/sites/${id}` : null)
  const snap = live.data

  useEffect(() => {
    setInfo(null)
    api<SiteInfo>(`/api/sites/${id}`).then(setInfo).catch(() => setError('Объект не найден.'))
  }, [id])
  useEffect(() => {
    history.replaceState(null, '', `#${tab}`)
  }, [tab])
  useEffect(() => {
    if (info) document.title = `${info.title} · Умные переходы`
  }, [info])

  if (error) return <div className="content"><p>{error} <Link to="/">Вернуться к карте</Link></p></div>
  if (!info) return <div className="content empty">Загружаю объект…</div>
  const near = info.addresses[0]

  return (
    <div className="site-page">
      <div className="topbar">
        <Link to={`/?site=${info.id}`} className="btn" aria-label="К карте">← Карта</Link>
        <Glyph kind={info.kind} status={info.equipped ? snap?.status || 'warn' : 'off'} />
        <div className="title">
          <h1>{info.title}</h1>
          <div className="sub">
            {KIND_TITLES[info.kind]} · {info.streets.join(', ')}
            {near && <> · рядом: {near.address}</>}
          </div>
        </div>
        <div className="right">
          {!info.equipped && <span className="chip off"><i />Не оснащён</span>}
          {snap && <span className={`chip ${snap.status}`}><i />{STATUS_TITLES[snap.status]}: {snap.mode_title}</span>}
          {info.equipped && !live.connected && <span className="chip">нет связи с центром</span>}
        </div>
      </div>
      {!info.equipped ? (
        <main className="content">
          <div className="info-page">
            <section className="card">
              <header><h3>Объект не оснащён</h3></header>
              <div className="body">
                <p>
                  Светофорный объект есть, но камер и связи с системой на нём нет: дорожный контроллер работает по своей
                  фиксированной программе. Чтобы включить объект в систему, нужно установить две широкоугольные камеры
                  и подключить контроллер к центру.
                </p>
              </div>
            </section>
            <section className="card">
              <header><h3>Паспорт</h3></header>
              <div className="body">
                <dl className="kv">
                  <dt>Тип</dt><dd>{KIND_TITLES[info.kind]}</dd>
                  <dt>Улицы</dt><dd>{info.streets.join(', ')}</dd>
                  <dt>Пешеходных переходов</dt><dd>{info.crosswalks.length}</dd>
                  <dt>Координаты</dt><dd>{info.lat.toFixed(5)}, {info.lon.toFixed(5)}</dd>
                </dl>
              </div>
            </section>
            <section className="card">
              <header><h3>Дома рядом</h3></header>
              <div className="body">
                <table className="t"><tbody>
                  {info.addresses.map((a) => <tr key={a.address}><td>{a.address}</td><td>{a.distance_m} м</td></tr>)}
                </tbody></table>
              </div>
            </section>
          </div>
        </main>
      ) : <>
      <nav className="tabs" role="tablist">
        {TABS.map(([t, title]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>{title}</button>
        ))}
      </nav>
      <main className="content">
        {tab === 'monitor' && <Monitoring info={info} snap={snap} />}
        {tab === 'stats' && <StatsTab siteId={info.id} />}
        {tab === 'settings' && info.params && <SettingsTab siteId={info.id} params={info.params} onSaved={(p) => setInfo({ ...info, params: p })} />}
      </main>
      </>}
    </div>
  )
}
