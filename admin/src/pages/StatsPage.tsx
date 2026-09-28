import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fmt, pct, timeOf, useOverview, type AreaStats } from '../api'
import { Shell } from '../components/Shell'
import { SiteList } from '../components/SiteList'
import { MobileSitePicker } from '../components/MobileSitePicker'

const RANGES: [number, string][] = [[24, 'Сутки'], [168, 'Неделя']]
const axis = { stroke: '#4a4740', tick: { fill: '#a8a193' } }
const tip = { contentStyle: { fontSize: 12 }, labelFormatter: (t: unknown) => String(t).slice(0, 16) }

export function StatsPage() {
  const { id } = useParams()
  const nav = useNavigate()
  const { sites, connected } = useOverview()
  const [hours, setHours] = useState(24)
  const [data, setData] = useState<AreaStats | null>(null)

  useEffect(() => {
    const load = () => api<AreaStats>(`/api/stats?hours=${hours}${id ? `&site=${id}` : ''}`).then(setData).catch(() => setData(null))
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [id, hours])

  const s = data?.summary
  // поток в машинах в минуту, как на макете
  const series = (data?.series || []).map((p) => ({ ...p, veh_pm: Math.round((p.veh_passed / (data?.bucket_min || 1)) * 10) / 10 }))
  const tf = (t: string) => (hours > 24 ? `${t.slice(8, 10)}.${t.slice(5, 7)} ${t.slice(11, 13)}ч` : t.slice(11, 16))
  const title = id ? sites.find((x) => x.id === id)?.title || id : 'Весь участок'
  const peak = data?.peak_hour
  const go = (anchor: string) => document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  return (
    <Shell connected={connected}>
      <div className="page with-right">
        <div className="stack">
          <MobileSitePicker sites={sites} selected={id} equippedOnly allLabel="Весь участок" mapLink={false}
                            onPick={(sid) => nav(sid ? `/stats/${sid}` : '/stats')} />
          <div className="mobile-only m-picker">
            <div className="seg" role="group" aria-label="Период">
              {RANGES.map(([h, t]) => <button key={h} className={h === hours ? 'on' : ''} onClick={() => setHours(h)}>{t}</button>)}
            </div>
          </div>
          <section className="panel" id="kpi">
            <div className="panel-title caps">Ключевые показатели · {title}</div>
            <div className="tiles">
              <div className="tile green"><div className="l">Всего машин</div><div className="v">{fmt(s?.veh_passed)}</div></div>
              <div className="tile green"><div className="l">Пешеходов перешли</div><div className="v">{fmt(s?.ped_served)} · ждали {fmt(s?.wait_avg, 1, 'с')}</div></div>
              <div className="tile yellow">
                <div className="l">Пиковый час</div>
                <div className="v">{peak ? `${peak.t.slice(11, 13)}:00 · ${fmt(peak.veh_flow_vph)} авт/ч` : '—'}</div>
              </div>
              <div className="tile red"><div className="l">Инцидентов сегодня</div><div className="v">{fmt(data?.incidents_today)}</div></div>
            </div>
          </section>

          <section className="panel" id="flow">
            <div className="chart-wrap">
              <div className="panel-title">Поток трафика за {hours > 24 ? 'неделю' : 'сутки (24ч)'} [машин/мин]</div>
              <div className="chart-main">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={series}>
                    <CartesianGrid stroke="#34322e" strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="t" tickFormatter={tf} minTickGap={40} {...axis} />
                    <YAxis width={40} {...axis} />
                    <Tooltip {...tip} />
                    <Line type="monotone" dataKey="veh_pm" name="машин/мин" stroke="#17b86c" dot={false} strokeWidth={2} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              {series.length === 0 && <div className="note">Данные появятся через минуту после запуска центра: статистика пишется поминутно.</div>}
            </div>
          </section>

          <section className="panel" id="peds">
            <div className="chart-wrap two">
              <div>
                <div className="panel-title">Ожидание пешеходов [с]</div>
                <div className="chart-sm">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={series}>
                      <CartesianGrid stroke="#34322e" strokeDasharray="3 3" vertical={false} />
                      <XAxis dataKey="t" tickFormatter={tf} minTickGap={40} {...axis} />
                      <YAxis width={34} {...axis} />
                      <Tooltip {...tip} />
                      <Legend />
                      <Line type="monotone" dataKey="wait_avg" name="среднее" stroke="#f2c21b" dot={false} strokeWidth={2} />
                      <Line type="monotone" dataKey="wait_max" name="максимум" stroke="#e0474f" dot={false} strokeWidth={1.5} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
              <div>
                <div className="panel-title">Пешеходы и пешеходные фазы</div>
                <div className="chart-sm">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={series}>
                      <CartesianGrid stroke="#34322e" strokeDasharray="3 3" vertical={false} />
                      <XAxis dataKey="t" tickFormatter={tf} minTickGap={40} {...axis} />
                      <YAxis width={34} {...axis} />
                      <Tooltip {...tip} />
                      <Legend />
                      <Bar dataKey="ped_served" name="перешли" fill="#17b86c" />
                      <Bar dataKey="ped_phases" name="фаз" fill="#f2c21b" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            </div>
            <div className="chart-wrap" style={{ marginTop: 18 }}>
              <dl className="kv" style={{ maxWidth: 520 }}>
                <dt>Переходили на красный</dt><dd>{pct(s?.violation_share, 1)}</dd>
                <dt>Машин останавливались</dt><dd>{pct(s?.stop_share)}</dd>
                <dt>Групп обслужено с приоритетом</dt><dd>{fmt(s?.groups)}</dd>
                <dt>Время в адаптивном режиме</dt>
                <dd>{s && s.ped_served + s.veh_passed > 0 ? pct(s.mode_share.adaptive) : '—'}</dd>
              </dl>
            </div>
          </section>

          {!id && (data?.by_site || []).length > 0 && (
            <section className="panel" id="objects">
              <div className="chart-wrap">
                <div className="panel-title">По объектам</div>
                <div className="tw">
                  <table className="t">
                    <thead><tr><th>Объект</th><th>Машин</th><th>Пешеходов</th><th>Ожидание, с</th><th>На красный</th><th>Остановки</th></tr></thead>
                    <tbody>
                      {data!.by_site.map((r) => (
                        <tr key={r.id} style={{ cursor: 'pointer' }} onClick={() => nav(`/stats/${r.id}`)}>
                          <td>{r.title}</td><td>{fmt(r.veh_passed)}</td><td>{fmt(r.ped_served)}</td>
                          <td>{fmt(r.wait_avg, 1)}</td><td>{pct(r.violation_share, 1)}</td><td>{pct(r.stop_share)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </section>
          )}

          <section className="panel" id="incidents">
            <div className="chart-wrap">
              <div className="panel-title">Инциденты</div>
              {(data?.incidents || []).length === 0 ? <div className="empty">Инцидентов нет.</div> :
                data!.incidents.map((e, i) => (
                  <div key={i} className={`event-row ${e.level}`} style={{ gridTemplateColumns: '120px 1fr' }}>
                    <time>{e.ts.slice(5, 10).split('-').reverse().join('.')} {timeOf(e.ts).slice(0, 5)}</time>
                    <span>{e.message} <span className="muted">· {e.site_title}</span></span>
                  </div>
                ))}
            </div>
          </section>

          <section className="panel" id="reports">
            <div className="chart-wrap">
              <div className="panel-title">Отчёты</div>
              <p className="side-text" style={{ margin: '0 0 10px' }}>Поминутная статистика ({title.toLowerCase()}) в CSV для Excel.</p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <a className="btn" href={`/api/stats/export.csv?hours=24${id ? `&site=${id}` : ''}`}>Скачать за сутки</a>
                <a className="btn" href={`/api/stats/export.csv?hours=168${id ? `&site=${id}` : ''}`}>Скачать за неделю</a>
              </div>
            </div>
          </section>
        </div>

        <aside className="sidebar desktop-only">
          <div className="side-h">Навигация по статистике</div>
          <button className="side-link" onClick={() => go('flow')}>Поток</button>
          <button className="side-link" onClick={() => go('peds')}>Пешеходы</button>
          <button className="side-link" onClick={() => go('incidents')}>Инциденты</button>
          <button className="side-link" onClick={() => go('reports')}>Отчёты</button>

          <div className="side-h">Период</div>
          <div className="seg" role="group" aria-label="Период">
            {RANGES.map(([h, t]) => <button key={h} className={h === hours ? 'on' : ''} onClick={() => setHours(h)}>{t}</button>)}
          </div>

          <div className="side-h">Объект</div>
          <button className={`side-item${!id ? ' on' : ''}`} onClick={() => nav('/stats')}><span className="dot ok" /><span>Весь участок</span></button>
          <SiteList sites={sites} selected={id} equippedOnly onPick={(sid) => nav(`/stats/${sid}`)} />
        </aside>
      </div>
    </Shell>
  )
}
