import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, lastSite, rememberSite, useOverview, type SiteInfo } from '../api'
import { SettingsTab } from '../components/SettingsTab'
import { Shell } from '../components/Shell'
import { SiteList } from '../components/SiteList'

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

  useEffect(() => {
    if (id || sites.length === 0) return
    const last = lastSite()
    const pick = sites.find((s) => s.id === last && s.equipped) || sites.find((s) => s.equipped)
    if (pick) nav(`/settings/${pick.id}`, { replace: true })
  }, [id, sites, nav])
  useEffect(() => {
    if (!id) return
    rememberSite(id)
    setInfo(null)
    api<SiteInfo>(`/api/sites/${id}`).then(setInfo).catch(() => setInfo(null))
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
            <div className="panel-title" style={{ margin: 0 }}>
              Параметры алгоритма{info ? ` · ${info.title}` : ''}
            </div>
            <div className="side-text" style={{ marginTop: 4 }}>Изменения применяются на объекте сразу, без перезапуска. Границы значений проверяет сервер.</div>
          </section>
          {info?.params
            ? <SettingsTab siteId={info.id} params={info.params} onSaved={(p) => setInfo({ ...info, params: p })} />
            : <div className="panel empty">Загружаю параметры…</div>}
        </div>
      </div>
    </Shell>
  )
}
