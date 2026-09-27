import { useState } from 'react'
import { fmt, post, timeOf, type SiteInfo, type Snapshot } from '../api'
import { Schematic } from './Schematic'

const SIGNAL_TITLES: Record<string, string> = {
  red: 'красный', red_yellow: 'красный с жёлтым', green: 'зелёный', green_blink: 'зелёный мигает',
  yellow: 'жёлтый', yellow_flash: 'жёлтый мигающий', off: 'выключен',
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
  const groups = info.layout.groups
  const veh = groups.filter((g) => g.kind === 'veh')
  const ped = groups.filter((g) => g.kind === 'ped')
  const o = snap?.observation
  const i = snap?.info || {}
  const stageTitle = (id: string) => info.layout.stages.find((s) => s.id === id)?.title || id

  return (
    <div className="mon">
      <div className="col">
        <section className="card">
          <header><h3>Схема объекта</h3><span className="chip demo">тестовый источник</span></header>
          <div className="body"><Schematic info={info} snap={snap} /></div>
          <p className="video-note">
            Видео с камер для этого объекта пока не подключено: данные идут от генератора, который выдаёт то же, что
            распознавание (ждущие пешеходы, очереди, подходящие машины). Контроллер и защита работают по-настоящему.
          </p>
        </section>
        <section className="card">
          <header><h3>Подходы транспорта</h3></header>
          <div className="body">
            <table className="t">
              <thead><tr><th>Подход</th><th>Сигнал</th><th>Очередь</th><th>Ближайшая</th><th>Интенсивность</th><th>Окно</th></tr></thead>
              <tbody>
                {veh.map((g) => (
                  <tr key={g.id}>
                    <td>{g.title}{o?.emergency.includes(g.id) && <span className="chip alarm" style={{ marginLeft: 8 }}>спецтранспорт</span>}</td>
                    <td><Lamp s={snap?.signals[g.id] || 'red'} /></td>
                    <td>{fmt(o?.queue[g.id])}</td>
                    <td>{o?.queue[g.id] == null ? '—' : o?.eta[g.id] == null ? 'нет' : fmt(o.eta[g.id], 1, 'с')}</td>
                    <td>{fmt(o?.flow_vph[g.id], 0, 'авт/ч')}</td>
                    <td>{o?.flow_vph[g.id] == null ? '—' : fmt(o?.flow_window_s[g.id], 0, 'с')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section className="card">
          <header><h3>Пешеходные переходы</h3></header>
          <div className="body">
            <table className="t">
              <thead><tr><th>Переход</th><th>Сигнал</th><th>Ждут</th><th>Дольше всех</th><th>Идут</th><th>Длина</th></tr></thead>
              <tbody>
                {ped.map((g) => (
                  <tr key={g.id}>
                    <td>{g.title}</td>
                    <td><Lamp s={snap?.signals[g.id] || 'red'} /></td>
                    <td>{o?.waiting[g.id] == null ? <span style={{ color: 'var(--alarm)' }}>не видно</span> : fmt(o.waiting[g.id])}</td>
                    <td>{fmt(o?.max_wait[g.id], 0, 'с')}</td>
                    <td>{fmt(o?.on_crosswalk[g.id])}</td>
                    <td>{fmt(g.length_m, 1, 'м')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <div className="col">
        <section className="card">
          <header><h3>Сейчас</h3><span className="mono muted">{fmt(snap?.stage_time_s, 0, 'с')}</span></header>
          <div className="body">
            {snap?.trip && (
              <div className="trip" style={{ marginBottom: 12 }}>
                <span>Сработала защита: {snap.trip}</span>
                <button className="btn" disabled={busy} onClick={() => act('/reset')}>Сбросить аварию</button>
              </div>
            )}
            <div className="now-stage">{snap ? (snap.next_stage ? `→ ${snap.next_stage}` : snap.stage) : '…'}</div>
            <div className="now-status">{snap?.status_text}</div>
            <div className="now-meta">
              <span className={`chip ${snap?.status || ''}`}><i />{snap?.mode_title}</span>
              <span className="muted" style={{ fontSize: 13 }}>{snap?.mode_reason}</span>
            </div>
          </div>
        </section>

        <section className="card">
          <header><h3>Что учитывает контроллер</h3></header>
          <div className="body">
            {info.kind === 'crossing' ? (
              <dl className="kv">
                <dt>Интенсивность по двум подходам</dt><dd>{fmt(i.flow_vph as number, 0, 'авт/ч')}</dd>
                <dt>Задержка D = D<sub>min</sub> + (D<sub>max</sub> − D<sub>min</sub>)·q/q<sub>нас</sub></dt><dd>{fmt(i.delay_s as number, 1, 'с')}</dd>
                <dt>С учётом группы</dt><dd>{i.group ? fmt(i.delay_eff_s as number, 1, 'с') : 'группы нет'}</dd>
                <dt>Ждут / дольше всех</dt><dd>{fmt(i.waiting as number)} / {fmt(i.max_wait_s as number, 0, 'с')}</dd>
                <dt>Разрыв в потоке</dt><dd>{i.gap ? 'есть' : 'нет'}</dd>
                <dt>Минимальный зелёный транспорту</dt><dd>{fmt(i.min_green_s as number, 0, 'с')}</dd>
              </dl>
            ) : (
              <dl className="kv">
                <dt>Интенсивность на всех подходах</dt><dd>{fmt(i.flow_vph as number, 0, 'авт/ч')}</dd>
                <dt>Минимальный зелёный</dt><dd>{fmt(i.min_green_s as number, 0, 'с')}</dd>
                <dt>Текущий зелёный длится</dt><dd>{fmt(i.green_s as number, 0, 'с')}</dd>
                <dt>Запрос от других фаз</dt>
                <dd>{Array.isArray(i.demand) && i.demand.length ? i.demand.map(stageTitle).join(', ') : 'нет'}</dd>
              </dl>
            )}
          </div>
        </section>

        <section className="card">
          <header><h3>Камеры</h3><span className="muted">{snap?.cameras_ok} из {snap?.cameras_total}</span></header>
          <div className="body">
            {(snap?.cameras || []).map((c) => (
              <div className="cam-row" key={c.id}>
                <div className="who"><span className={`dot${c.ok ? '' : ' bad'}`} /><span>{c.title}</span></div>
                <button className={`btn${c.ok ? ' danger' : ''}`} disabled={busy}
                        onClick={() => act(`/cameras/${c.id}`, { ok: !c.ok })}>
                  {c.ok ? 'Имитировать отказ' : 'Восстановить'}
                </button>
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <header><h3>Управление</h3></header>
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
            <div className="row">
              <span>Демонстрация</span>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button className="btn" disabled={busy} onClick={() => act('/demo/group', { size: 10 })}>Группа 10 человек</button>
                <button className="btn" disabled={busy} onClick={() => act('/demo/emergency')}>Спецтранспорт</button>
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
