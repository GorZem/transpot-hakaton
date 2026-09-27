import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, lastSite, rememberSite, useOverview, type SiteInfo } from '../api'
import { SettingsTab } from '../components/SettingsTab'
import { Shell } from '../components/Shell'
import { SiteList } from '../components/SiteList'
import { ZoneEditor } from '../components/ZoneEditor'

interface SystemInfo {
  equipment_url: string
  detector: { model: string; ready: boolean; error: string | null; ms_per_image: number }
}

export function SettingsPage() {
  const { id } = useParams()
  const nav = useNavigate()
  const { sites, connected } = useOverview()
  const [info, setInfo] = useState<SiteInfo | null>(null)
  const [sys, setSys] = useState<SystemInfo | null>(null)
  const [tab, setTab] = useState<'params' | 'zones'>(() => (location.hash === '#zones' ? 'zones' : 'params'))
  useEffect(() => { history.replaceState(null, '', tab === 'zones' ? '#zones' : location.pathname) }, [tab])

  useEffect(() => {
    if (id || sites.length === 0) return
    const last = lastSite()
    const pick = sites.find((s) => s.id === last && s.equipped) || sites.find((s) => s.equipped)
    if (pick) nav(`/settings/${pick.id}`, { replace: true })
  }, [id, sites, nav])
  useEffect(() => {
    if (!id) return
    rememberSite(id)
    let actual = true
    api<SiteInfo>(`/api/sites/${id}`).then((d) => { if (actual) setInfo(d) }).catch(() => {})
    return () => { actual = false }
  }, [id])
  useEffect(() => {
    api<SystemInfo>('/api/system').then(setSys).catch(() => {})
  }, [])

  return (
    <Shell connected={connected}>
      <div className="page with-left">
        <aside className="sidebar">
          <div className="side-h">Объект</div>
          <SiteList sites={sites} selected={id} equippedOnly onPick={(sid) => nav(`/settings/${sid}`)} />
          <div className="side-h">Система</div>
          <div className="side-text">Оборудование: {sys?.equipment_url || '…'}</div>
          <div className="side-text">
            Детектор: {sys ? `${sys.detector.model}, ${sys.detector.ready ? `${sys.detector.ms_per_image} мс/кадр` : sys.detector.error || 'загружается'}` : '…'}
          </div>
        </aside>
        <div className="stack">
          <section className="panel tight">
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <div className="panel-title" style={{ margin: 0 }}>Настройки объекта{info ? ` · ${info.title}` : ''}</div>
              <div className="seg tabs-seg" role="tablist" style={{ marginLeft: 'auto' }}>
                <button role="tab" aria-selected={tab === 'params'} className={tab === 'params' ? 'on' : ''} onClick={() => setTab('params')}>Параметры алгоритма</button>
                <button role="tab" aria-selected={tab === 'zones'} className={tab === 'zones' ? 'on' : ''} onClick={() => setTab('zones')}>Зоны камер</button>
              </div>
            </div>
            <div className="side-text" style={{ marginTop: 4 }}>
              {tab === 'params' ? 'Изменения применяются на объекте сразу, без перезапуска. Границы значений проверяет сервер.'
                : 'Зоны, по которым система понимает, кто ждёт у перехода, кто идёт по нему и какие машины подъезжают. Размечаются на кадре каждой камеры.'}
            </div>
          </section>
          {tab === 'zones' ? (info ? <ZoneEditor key={info.id} siteId={info.id} /> : null)
            : info?.params
              ? <SettingsTab siteId={info.id} params={info.params} onSaved={(p) => setInfo({ ...info, params: p })} />
              : <div className="panel empty">Загружаю параметры…</div>}
        </div>
      </div>
    </Shell>
  )
}
