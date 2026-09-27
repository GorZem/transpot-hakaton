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
  const live = useLive<Snapshot>(info ? `/ws/sites/${id}` : null)
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
        <Glyph kind={info.kind} status={snap?.status || 'ok'} />
        <div className="title">
          <h1>{info.title}</h1>
          <div className="sub">
            {KIND_TITLES[info.kind]} · {info.streets.join(', ')}
            {near && <> · рядом: {near.address}</>}
          </div>
        </div>
        <div className="right">
          {snap && <span className={`chip ${snap.status}`}><i />{STATUS_TITLES[snap.status]}: {snap.mode_title}</span>}
          {!live.connected && <span className="chip">нет связи с центром</span>}
          <span className="chip demo">тестовый источник данных</span>
        </div>
      </div>
      <nav className="tabs" role="tablist">
        {TABS.map(([t, title]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>{title}</button>
        ))}
      </nav>
      <main className="content">
        {tab === 'monitor' && <Monitoring info={info} snap={snap} />}
        {tab === 'stats' && <StatsTab siteId={info.id} />}
        {tab === 'settings' && <SettingsTab siteId={info.id} params={info.params} onSaved={(p) => setInfo({ ...info, params: p })} />}
      </main>
    </div>
  )
}
