import { useEffect, useRef, useState } from 'react'
import { api, type SearchHit } from '../api'

/** Поиск по пересечению улиц, названию объекта или адресу ближайшего дома. */
export function SearchBox({ onPick }: { onPick: (hit: SearchHit) => void }) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<SearchHit[]>([])
  const [open, setOpen] = useState(false)
  const [act, setAct] = useState(0)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!q.trim()) {
      setHits([])
      return
    }
    const ctl = new AbortController()
    const t = setTimeout(() => {
      api<SearchHit[]>(`/api/search?q=${encodeURIComponent(q)}`, { signal: ctl.signal })
        .then((h) => {
          setHits(h)
          setAct(0)
        })
        .catch(() => {})
    }, 120)
    return () => {
      clearTimeout(t)
      ctl.abort()
    }
  }, [q])

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const pick = (h: SearchHit) => {
    onPick(h)
    setOpen(false)
    setQ(h.label)
  }

  return (
    <div className="search" ref={box}>
      <svg className="lens" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
        <circle cx="7" cy="7" r="5" />
        <path d="M11 11l3.5 3.5" strokeLinecap="round" />
      </svg>
      <input
        id="search"
        type="search"
        placeholder="Пересечение улиц или адрес дома"
        value={q}
        autoComplete="off"
        onChange={(e) => {
          setQ(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') setAct((a) => Math.min(a + 1, hits.length - 1))
          else if (e.key === 'ArrowUp') setAct((a) => Math.max(a - 1, 0))
          else if (e.key === 'Enter' && hits[act]) pick(hits[act])
          else if (e.key === 'Escape') setOpen(false)
        }}
      />
      {open && q.trim() && (
        <ul role="listbox">
          {hits.length === 0 && <li className="none">Ничего не нашлось. Попробуйте «Краснодарская Краснодонская» или «Судакова 17».</li>}
          {hits.map((h, i) => (
            <li key={`${h.type}-${h.label}-${h.site_id}`}>
              <button className={i === act ? 'act' : ''} onMouseEnter={() => setAct(i)} onClick={() => pick(h)}>
                <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5" style={{ marginTop: 2, color: 'var(--muted)' }}>
                  {h.type === 'site'
                    ? <><rect x="5" y="1.5" width="6" height="13" rx="2" /><circle cx="8" cy="5" r="1.2" /><circle cx="8" cy="8" r="1.2" /><circle cx="8" cy="11" r="1.2" /></>
                    : <path d="M2.5 7.5 8 3l5.5 4.5V14h-11z M6.5 14v-3.5h3V14" strokeLinejoin="round" />}
                </svg>
                <span>
                  {h.label}
                  <small>{h.sublabel}</small>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
