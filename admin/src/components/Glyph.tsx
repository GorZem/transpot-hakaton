import type { Kind } from '../api'

/** Значок типа объекта: переход (зебра), Т-образный, крестовой. */
export function glyphSvg(kind: Kind, color = '#fff'): string {
  const s = `stroke="${color}" stroke-width="3" stroke-linecap="round" fill="none"`
  if (kind === 'cross') return `<svg viewBox="0 0 20 20"><path d="M3 10H17M10 3V17" ${s}/></svg>`
  if (kind === 'tee') return `<svg viewBox="0 0 20 20"><path d="M3 5H17M10 5V17" ${s}/></svg>`
  return `<svg viewBox="0 0 20 20"><g fill="${color}"><rect x="3" y="3" width="3" height="14" rx="1"/><rect x="8.5" y="3" width="3" height="14" rx="1"/><rect x="14" y="3" width="3" height="14" rx="1"/></g></svg>`
}

export function Glyph({ kind, status, size = 34 }: { kind: Kind; status: string; size?: number }) {
  return (
    <span
      className={`marker ${status}`}
      style={{ width: size, height: size, borderWidth: 2, boxShadow: 'none', animation: 'none' }}
      dangerouslySetInnerHTML={{ __html: glyphSvg(kind) }}
    />
  )
}
