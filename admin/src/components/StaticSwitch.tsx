import { useState } from 'react'
import { post } from '../api'

/**
 * Переключатель «Статический режим»: светофор работает по штатной (фиксированной) программе
 * дорожного контроллера, система фазы не переключает. Статический режим включается и сам,
 * если хоть одна камера объекта непригодна, — тогда это показано под переключателем.
 */
export function StaticSwitch({ siteId, value, reason, onDone }: {
  siteId: string; value: 'operator' | 'auto' | null | undefined; reason?: string | null; onDone?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const on = value === 'operator'
  const toggle = async () => {
    setBusy(true)
    try { await post(`/api/sites/${siteId}/static`, { on: !on }); onDone?.() } finally { setBusy(false) }
  }
  return (
    <div className="static-box">
      <label className="switch" title="Светофор работает по штатной программе дорожного контроллера">
        <input type="checkbox" checked={on} disabled={busy || value === undefined} onChange={toggle} />
        <i aria-hidden /> Статический режим
      </label>
      {value === 'auto' && (
        <div className="static-auto">Включён автоматически: {reason || 'камера непригодна'}. Вернётся к умному режиму сам, когда камеры восстановятся.</div>
      )}
    </div>
  )
}
