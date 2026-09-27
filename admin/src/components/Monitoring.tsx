import { useState } from 'react'
import { fmt, post, timeOf, type SiteInfo, type Snapshot } from '../api'
import { Schematic } from './Schematic'

const SIGNAL_TITLES: Record<string, string> = {
  red: 'красный', red_yellow: 'красный с жёлтым', green: 'зелёный', green_blink: 'зелёный мигает',
  yellow: 'жёлтый', yellow_flash: 'жёлтый мигающий', off: 'выключен',
}
const DEVICE_MODES: Record<string, string> = {
  remote: 'управляет система', local: 'своя программа', flash: 'жёлтый мигающий (своя защита)',
}

function Lamp({ s }: { s: string }) {
  const color = s === 'red' ? 'red' : s.startsWith('green') ? 'green' : s === 'off' ? '' : 'yellow'
  const blink = s === 'green_blink' || s === 'yellow_flash'
  return <i className={`lamp ${color}${blink ? ' blink' : ''}`} title={SIGNAL_TITLES[s] || s} />
}

const MODES: [string | null, string][] = [[null, 'Авто'], ['fixed', 'Фикс. план'], ['flashing', 'Жёлтый мигающий']]

export function Monitoring({ info, snap }: { info: SiteInfo; snap: Snapshot | null }) {
  const [busy, setBusy] = useState(false)
  const act = async (path: string, body: unknown = {}) => {
    setBusy(true)
    try {
      await post(`/api/sites/${info.id}${path}`, body)
    } finally {
      setBusy(false)
    }
  }
  const groups = info.layout?.groups || []
  const veh = groups.filter((g) => g.kind === 'veh')
  const ped = groups.filter((g) => g.kind === 'ped')
  const o = snap?.observation
  const i = snap?.info || {}
  const stageTitle = (id: string) => info.layout?.stages.find((s) => s.id === id)?.title || id
  const eq = snap?.equipment

  return (
    <div className="mon">
      <div className="col">
        <section className="card">
          <header><h3>Камеры</h3><span className="muted">{snap?.cameras_ok ?? '…'} из {snap?.cameras_total ?? info.cameras.length} исправны</span></header>
          <div className="body cams">
            {info.cameras.map((c) => {
              const st = snap?.cameras.find((x) => x.id === c.id)
              return (
                <figure key={c.id} className="cam">
                  <img src={`/video/${c.id}.mjpg`} alt={c.title} loading="lazy" />
                  <figcaption>
                    <span className={`dot${st && !st.ok ? ' bad' : ''}`} />
                    <span>{c.title}</span>
                    <span className="muted mono">{st ? (st.ok ? `${fmt(st.fps, 1)} к/с` : st.fault_title) : ''}</span>
                  </figcaption>
                </figure>
              )
            })}
          </div>
          <p className="video-note">
            Зоны на кадре: переход (цвет сигнала пешеходам), зоны ожидания на тротуарах, подходы транспорта.
            Рамки: жёлтые — пешеходы, оранжевые — транспорт, красные — спецтранспорт с маячком.
            Широкоугольный объектив около {fmt(info.cameras[0]?.hfov_deg)}°, высота подвеса {fmt(info.cameras[0]?.height_m, 1)} м.
          </p>
        </section>
        <section className="card">
          <header><h3>Схема объекта</h3></header>
          <div className="body"><Schematic info={info} snap={snap} /></div>
        </section>
      </div>

      <div className="col">
        <section className="card">
          <header><h3>Сейчас</h3><span className="mono muted">{snap?.stage_time_s != null ? fmt(snap.stage_time_s, 0, 'с') : ''}</span></header>
          <div className="body">
            {snap?.trip && (
              <div className="trip" style={{ marginBottom: 12 }}>
                <span>Сработала защита: {snap.trip}</span>
                <button className="btn" disabled={busy} onClick={() => act('/reset')}>Сбросить аварию</button>
              </div>
            )}
            <div className="now-stage">{snap ? (snap.engaged ? (snap.next_stage ? `→ ${snap.next_stage}` : snap.stage) : snap.mode_title) : '…'}</div>
            <div className="now-status">{snap?.status_text}</div>
            <div className="now-meta">
              <span className={`chip ${snap?.status || ''}`}><i />{snap?.mode_title}</span>
              {snap?.engaged && <span className="muted" style={{ fontSize: 13 }}>{snap?.mode_reason}</span>}
            </div>
          </div>
        </section>

        <section className="card">
          <header><h3>Контроллер объекта</h3></header>
          <div className="body">
            <dl className="kv">
              <dt>Связь</dt><dd style={{ color: eq?.connected ? 'var(--ok)' : 'var(--alarm)' }}>{eq ? (eq.connected ? 'есть' : 'нет') : '…'}</dd>
              <dt>Режим контроллера</dt><dd>{eq?.mode ? DEVICE_MODES[eq.mode] || eq.mode : '—'}</dd>
              {eq?.error && <><dt>Ошибка</dt><dd style={{ whiteSpace: 'normal' }}>{eq.error}</dd></>}
              {eq?.rejected && <><dt>Отклонена команда</dt><dd style={{ whiteSpace: 'normal' }}>{eq.rejected}</dd></>}
            </dl>
            <table className="t" style={{ marginTop: 10 }}>
              <thead><tr><th>Группа сигналов</th><th>Сигнал</th></tr></thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.id}><td>{g.title}</td><td><Lamp s={snap?.signals[g.id] || 'red'} /> {SIGNAL_TITLES[snap?.signals[g.id] || ''] || '—'}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card">
          <header><h3>Что видит система</h3></header>
          <div className="body">
            <table className="t">
              <thead><tr><th>Пешеходы</th><th>Ждут</th><th>Дольше всех</th><th>Идут</th></tr></thead>
              <tbody>
                {ped.map((g) => (
                  <tr key={g.id}>
                    <td>{g.title}</td>
                    <td>{o?.waiting[g.id] == null ? <span style={{ color: 'var(--alarm)' }}>не видно</span> : fmt(o.waiting[g.id])}</td>
                    <td>{fmt(o?.max_wait[g.id], 0, 'с')}</td>
                    <td>{fmt(o?.on_crosswalk[g.id])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="t" style={{ marginTop: 12 }}>
              <thead><tr><th>Транспорт</th><th>Очередь</th><th>Ближайшая</th><th>Поток</th></tr></thead>
              <tbody>
                {veh.map((g) => (
                  <tr key={g.id}>
                    <td>{g.title}{o?.emergency.includes(g.id) && <span className="chip alarm" style={{ marginLeft: 8 }}>спецтранспорт</span>}</td>
                    <td>{o?.queue[g.id] == null ? <span style={{ color: 'var(--alarm)' }}>не видно</span> : fmt(o.queue[g.id])}</td>
                    <td>{o?.queue[g.id] == null ? '—' : o?.eta[g.id] == null ? 'нет' : fmt(o.eta[g.id], 1, 'с')}</td>
                    <td>{fmt(o?.flow_vph[g.id], 0, 'авт/ч')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {snap?.engaged && (info.kind === 'crossing' ? (
              <dl className="kv" style={{ marginTop: 12 }}>
                <dt>Задержка D = D<sub>min</sub> + (D<sub>max</sub> − D<sub>min</sub>)·q/q<sub>нас</sub></dt><dd>{fmt(i.delay_s as number, 1, 'с')}</dd>
                <dt>С учётом группы</dt><dd>{i.group ? fmt(i.delay_eff_s as number, 1, 'с') : 'группы нет'}</dd>
                <dt>Разрыв в потоке</dt><dd>{i.gap ? 'есть' : 'нет'}</dd>
              </dl>
            ) : (
              <dl className="kv" style={{ marginTop: 12 }}>
                <dt>Текущий зелёный длится</dt><dd>{fmt(i.green_s as number, 0, 'с')}</dd>
                <dt>Запрос от другой фазы</dt>
                <dd>{Array.isArray(i.demand) && i.demand.length ? i.demand.map(stageTitle).join(', ') : 'нет'}</dd>
              </dl>
            ))}
          </div>
        </section>

        <section className="card">
          <header><h3>Режим оператора</h3></header>
          <div className="body controls">
            <div className="row">
              <span>Режим работы</span>
              <div className="seg" role="group" aria-label="Режим работы">
                {MODES.map(([m, t]) => (
                  <button key={t} className={(snap?.forced ?? null) === m ? 'on' : ''} disabled={busy}
                          onClick={() => act('/mode', { mode: m })}>{t}</button>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section className="card">
          <header><h3>Журнал решений</h3></header>
          <div className="body log">
            {(snap?.events || []).length === 0 && <div className="empty">Событий пока нет.</div>}
            {(snap?.events || []).map((e, k) => (
              <div key={k} className={`event-row ${e.level}`}>
                <time>{timeOf(e.ts)}</time>
                <span>{e.message}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}
