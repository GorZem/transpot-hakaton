import { useEffect, useRef, useState } from 'react'

/**
 * Видео камеры отдельными кадрами (JPEG по очереди), а не постоянным MJPEG-соединением.
 * Браузер держит не больше 6 соединений к одному серверу: несколько MJPEG-потоков и WebSocket
 * занимали их все, и при смене объекта запросы данных вставали в очередь.
 * Здесь каждый кадр — короткий запрос; следующий запрашивается после загрузки предыдущего.
 */
export function CameraView({ camId, alt, fps = 5 }: { camId: string; alt: string; fps?: number }) {
  const [src, setSrc] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const current = useRef<string | null>(null)

  useEffect(() => {
    let alive = true
    let timer: number | undefined
    let fails = 0
    const period = 1000 / fps
    const next = () => {
      if (!alive) return
      if (document.hidden) {
        timer = window.setTimeout(next, 500)
        return
      }
      const t0 = performance.now()
      const url = `/video/${camId}.jpg?t=${Date.now()}`
      const img = new Image()
      img.onload = () => {
        if (!alive) return
        fails = 0
        setStale(false)
        if (current.current?.startsWith('blob:')) URL.revokeObjectURL(current.current)
        current.current = url
        setSrc(url)
        timer = window.setTimeout(next, Math.max(0, period - (performance.now() - t0)))
      }
      img.onerror = () => {
        if (!alive) return
        fails += 1
        if (fails > 2) setStale(true)
        timer = window.setTimeout(next, Math.min(3000, 400 * fails))
      }
      img.src = url
    }
    setSrc(null)
    next()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [camId, fps])

  return (
    <div className="cam-frame">
      {src ? <img src={src} alt={alt} /> : <div className="cam-wait">Подключаю камеру…</div>}
      {stale && <div className="cam-stale">нет кадров с камеры</div>}
    </div>
  )
}
