import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fmt, pct, type Stats } from '../api'

const RANGES: [number, string][] = [[6, '6 часов'], [24, 'Сутки'], [168, 'Неделя']]
const MODE_TITLES: Record<string, string> = { adaptive: 'адаптивный', degraded: 'деградированный', fixed: 'фиксированный', flashing: 'жёлтый мигающий' }

const axis = { stroke: 'var(--line)', tick: { fill: 'var(--muted)' } }

function tickTime(hours: number) {
  return (t: string) => (hours > 24 ? `${t.slice(8, 10)}.${t.slice(5, 7)} ${t.slice(11, 13)}ч` : t.slice(11, 16))
}

function Box({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <section className="card">
      <header><h3>{title}</h3></header>
      <div className="body chart-box"><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div>
    </section>
  )
}

export function StatsTab({ siteId }: { siteId: string }) {
  const [hours, setHours] = useState(24)
  const [data, setData] = useState<Stats | null>(null)
  useEffect(() => {
    const load = () => api<Stats>(`/api/sites/${siteId}/stats?hours=${hours}`).then(setData).catch(() => {})
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [siteId, hours])

  const s = data?.summary
  const series = data?.series || []
  const tf = tickTime(hours)
  const tooltip = { labelFormatter: (t: unknown) => String(t).slice(0, 16), contentStyle: { fontSize: 12 } }

  return (
    <div>
      <div className="stats-head">
        <div className="seg" role="group" aria-label="Период">
          {RANGES.map(([h, t]) => <button key={h} className={h === hours ? 'on' : ''} onClick={() => setHours(h)}>{t}</button>)}
        </div>
      </div>
      <div className="tiles">
        <div className="tile"><div className="v">{fmt(s?.ped_served)}</div><div className="l">пешеходов перешли</div></div>
        <div className="tile"><div className="v">{fmt(s?.wait_avg, 1, 'с')}</div><div className="l">среднее ожидание</div></div>
        <div className="tile"><div className="v">{fmt(s?.wait_max, 0, 'с')}</div><div className="l">максимальное ожидание</div></div>
        <div className="tile"><div className="v">{pct(s?.violation_share, 1)}</div><div className="l">перешли на красный</div></div>
        <div className="tile"><div className="v">{fmt(s?.groups)}</div><div className="l">групп обслужено с приоритетом</div></div>
        <div className="tile"><div className="v">{fmt(s?.veh_passed)}</div><div className="l">машин проехало</div></div>
        <div className="tile"><div className="v">{pct(s?.stop_share)}</div><div className="l">машин останавливались</div></div>
        <div className="tile">
          <div className="v">{pct(s?.mode_share?.adaptive)}</div>
          <div className="l">
            времени в адаптивном режиме
            {s && Object.entries(s.mode_share).filter(([k, v]) => k !== 'adaptive' && v > 0.001).map(([k, v]) => (
              <div key={k}>{MODE_TITLES[k]}: {pct(v, 1)}</div>
            ))}
          </div>
        </div>
      </div>
      <div className="charts">
        <Box title="Ожидание пешеходов, с">
          <LineChart data={series}>
            <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="t" tickFormatter={tf} minTickGap={40} {...axis} />
            <YAxis width={36} {...axis} />
            <Tooltip {...tooltip} />
            <Legend />
            <Line type="monotone" dataKey="wait_avg" name="среднее" stroke="var(--accent)" dot={false} strokeWidth={2} />
            <Line type="monotone" dataKey="wait_max" name="максимум" stroke="var(--warn)" dot={false} strokeWidth={1.5} />
          </LineChart>
        </Box>
        <Box title="Пешеходы и пешеходные фазы">
          <BarChart data={series}>
            <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="t" tickFormatter={tf} minTickGap={40} {...axis} />
            <YAxis width={40} {...axis} />
            <Tooltip {...tooltip} />
            <Legend />
            <Bar dataKey="ped_served" name="перешли" fill="var(--accent)" />
            <Bar dataKey="ped_phases" name="пешеходных фаз" fill="var(--ok)" />
          </BarChart>
        </Box>
        <Box title="Интенсивность транспорта, авт/ч">
          <LineChart data={series}>
            <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="t" tickFormatter={tf} minTickGap={40} {...axis} />
            <YAxis width={44} {...axis} />
            <Tooltip {...tooltip} />
            <Line type="monotone" dataKey="veh_flow_vph" name="авт/ч" stroke="var(--accent)" dot={false} strokeWidth={2} />
          </LineChart>
        </Box>
        <Box title="Распределение ожидания (по минутам)">
          <BarChart data={(data?.wait_hist || []).map((h) => ({ ...h, label: `${h.from_s}–${h.from_s + 10} с` }))}>
            <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" {...axis} />
            <YAxis width={44} {...axis} />
            <Tooltip contentStyle={{ fontSize: 12 }} />
            <Bar dataKey="count" name="пешеходов" fill="var(--accent)" />
          </BarChart>
        </Box>
      </div>
    </div>
  )
}
