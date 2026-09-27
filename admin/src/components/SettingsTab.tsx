import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

interface Prop {
  title: string
  description: string
  default: number
  minimum?: number
  maximum?: number
  type: string
  group: string
  unit: string
}

export function SettingsTab({ siteId, params, onSaved }: { siteId: string; params: Record<string, number>; onSaved: (p: Record<string, number>) => void }) {
  const [schema, setSchema] = useState<Record<string, Prop> | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [errs, setErrs] = useState<Record<string, string>>({})

  useEffect(() => {
    api<{ properties: Record<string, Prop> }>('/api/params/schema').then((s) => setSchema(s.properties))
  }, [])
  useEffect(() => {
    setValues(Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])))
  }, [params])

  const groups = useMemo(() => {
    const out = new Map<string, [string, Prop][]>()
    Object.entries(schema || {}).forEach(([k, p]) => {
      if (!out.has(p.group)) out.set(p.group, [])
      out.get(p.group)!.push([k, p])
    })
    return [...out.entries()]
  }, [schema])

  const changed = Object.entries(values).some(([k, v]) => Number(v) !== params[k])

  const save = async () => {
    setMsg(null)
    setErrs({})
    const body = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, Number(v)]))
    try {
      const saved = await api<Record<string, number>>(`/api/sites/${siteId}/params`, { method: 'PUT', body: JSON.stringify(body) })
      onSaved(saved)
      setMsg({ ok: true, text: 'Сохранено. Узел применил параметры без перезапуска.' })
    } catch (e) {
      const detail = (e as { detail?: { field: string; message: string }[] }).detail
      if (Array.isArray(detail)) setErrs(Object.fromEntries(detail.map((d) => [d.field.split('.').pop() || d.field, d.message])))
      setMsg({ ok: false, text: 'Не сохранено: проверьте поля, выделенные красным.' })
    }
  }

  if (!schema) return <div className="empty">Загружаю параметры…</div>
  return (
    <div className="stack" style={{ overflow: 'visible' }}>
      {groups.map(([g, props]) => (
        <section className="panel" key={g}>
          <div className="panel-title caps">{g}</div>
          <div>
            {props.map(([k, p]) => (
              <div className="field" key={k}>
                <label htmlFor={`p-${k}`}>{p.title}</label>
                <p>{p.description}</p>
                <div className="in">
                  <div>
                    <input id={`p-${k}`} type="number" step={p.type === 'integer' ? 1 : 0.05} className={errs[k] ? 'err' : ''}
                           value={values[k] ?? ''} min={p.minimum} max={p.maximum}
                           onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))} />
                    <span className="unit">{p.unit}</span>
                  </div>
                  <span className="range">{errs[k] || `${p.minimum ?? '−∞'} … ${p.maximum ?? '∞'}, по умолчанию ${p.default}`}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
      <div className="save-bar">
        <span className={`msg ${msg?.ok ? 'ok' : 'bad'}`}>{msg?.text || (changed ? 'Есть несохранённые изменения.' : 'Изменений нет.')}</span>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" disabled={!changed} onClick={() => setValues(Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])))}>Отменить</button>
          <button className="btn primary" disabled={!changed} onClick={save}>Сохранить</button>
        </div>
      </div>
    </div>
  )
}
