import type { ReactNode } from 'react'
import type { SiteInfo, Snapshot } from '../api'

/**
 * Схема объекта сверху: дороги, переходы, сигналы, очереди машин, ждущие пешеходы, обзор камер.
 * Рисуется в условной ориентации (главная дорога вертикально), подписи подходов — настоящие.
 */

const W = 440
type Dir = 'down' | 'up' | 'left' | 'right'

function VehHead({ x, y, s }: { x: number; y: number; s: string }) {
  const red = s === 'red' || s === 'red_yellow'
  const yellow = s === 'yellow' || s === 'red_yellow' || s === 'yellow_flash'
  const green = s === 'green' || s === 'green_blink'
  return (
    <g>
      <rect x={x - 7} y={y - 19} width={14} height={38} rx={4} fill="#22272b" stroke="var(--faint)" strokeWidth={1} />
      <circle cx={x} cy={y - 11} r={4.5} fill={red ? 'var(--lamp-red)' : 'var(--lamp-off)'} />
      <circle cx={x} cy={y} r={4.5} fill={yellow ? 'var(--lamp-yellow)' : 'var(--lamp-off)'} className={s === 'yellow_flash' ? 'blink' : ''} />
      <circle cx={x} cy={y + 11} r={4.5} fill={green ? 'var(--lamp-green)' : 'var(--lamp-off)'} className={s === 'green_blink' ? 'blink' : ''} />
    </g>
  )
}

function PedHead({ x, y, s }: { x: number; y: number; s: string }) {
  return (
    <g>
      <rect x={x - 6} y={y - 10} width={12} height={20} rx={3} fill="#22272b" stroke="var(--faint)" strokeWidth={1} />
      <rect x={x - 3.5} y={y - 7.5} width={7} height={6} rx={1} fill={s === 'red' ? 'var(--lamp-red)' : 'var(--lamp-off)'} />
      <rect x={x - 3.5} y={y + 1.5} width={7} height={6} rx={1}
            fill={s === 'green' || s === 'green_blink' ? 'var(--lamp-green)' : 'var(--lamp-off)'}
            className={s === 'green_blink' ? 'blink' : ''} />
    </g>
  )
}

/** Очередь у стоп-линии. (x, y) — стоп-линия, машины выстраиваются против направления движения. */
function Queue({ x, y, dir, n, emergency }: { x: number; y: number; dir: Dir; n: number | null; emergency: boolean }) {
  if (n == null) {
    const [tx, ty] = dir === 'down' ? [x, y - 30] : dir === 'up' ? [x, y + 38] : dir === 'right' ? [x - 40, y + 4] : [x + 40, y + 4]
    return <text x={tx} y={ty} textAnchor="middle" fontSize={11} fill="var(--alarm)">не видно</text>
  }
  const shown = Math.min(n, 6)
  const cars: ReactNode[] = []
  const vert = dir === 'down' || dir === 'up'
  const [cw, ch] = vert ? [14, 24] : [24, 14]
  for (let i = 0; i < shown; i++) {
    const off = 4 + i * 29
    const rx = dir === 'down' ? x - 7 : dir === 'up' ? x - 7 : dir === 'right' ? x - off - 24 : x + off
    const ry = dir === 'down' ? y - off - 24 : dir === 'up' ? y + off : y - 7
    const em = emergency && i === 0
    cars.push(<rect key={i} x={rx} y={ry} width={cw} height={ch} rx={3} fill={em ? '#f4f4f2' : 'var(--car)'} stroke={em ? 'var(--alarm)' : 'none'} strokeWidth={2} />)
  }
  if (n > shown) {
    const off = 4 + shown * 29 + 8
    const [tx, ty] = dir === 'down' ? [x, y - off] : dir === 'up' ? [x, y + off + 8] : dir === 'right' ? [x - off - 6, y + 4] : [x + off + 6, y + 4]
    cars.push(<text key="more" x={tx} y={ty} textAnchor="middle" fontSize={11} fill="var(--muted)" className="mono">+{n - shown}</text>)
  }
  return <g>{cars}</g>
}

function Zebra({ x, y, w, h, across }: { x: number; y: number; w: number; h: number; across: 'v' | 'h' }) {
  const bars: ReactNode[] = []
  if (across === 'v') for (let bx = x + 4; bx < x + w - 3; bx += 11) bars.push(<rect key={bx} x={bx} y={y} width={6} height={h} fill="var(--zebra)" />)
  else for (let by = y + 4; by < y + h - 3; by += 11) bars.push(<rect key={by} x={x} y={by} width={w} height={6} fill="var(--zebra)" />)
  return <g opacity={0.9}>{bars}</g>
}

function Badge({ x, y, n, wait, walking }: { x: number; y: number; n: number | null | undefined; wait: number | null | undefined; walking: number | null | undefined }) {
  const text = n == null ? 'не видно' : `${n} ждут${n ? ` · ${Math.round(wait || 0)} с` : ''}`
  const sub = walking ? `${walking} на переходе` : ''
  const w = Math.max(text.length, sub.length) * 6.6 + 16
  const h = sub ? 34 : 20
  const bad = n == null
  return (
    <g>
      <rect x={x - w / 2} y={y - 10} width={w} height={h} rx={10} fill={bad ? 'var(--alarm-soft)' : n ? 'var(--accent)' : 'var(--panel)'} stroke="var(--line)" />
      <text x={x} y={y + 4} textAnchor="middle" fontSize={11.5} fontWeight={500} fill={bad ? 'var(--alarm)' : n ? '#fff' : 'var(--muted)'}>{text}</text>
      {sub && <text x={x} y={y + 18} textAnchor="middle" fontSize={11} fill={n ? '#fff' : 'var(--muted)'}>{sub}</text>}
    </g>
  )
}

function Cam({ x, y, angle, ok, label }: { x: number; y: number; angle: number; ok: boolean; label: string }) {
  // Широкоугольная камера: сектор 110°, дальность в масштабе схемы.
  const r = 170
  const pts = [[x, y]]
  for (let d = -55; d <= 55; d += 5) {
    const a = ((angle + d) * Math.PI) / 180
    pts.push([x + r * Math.cos(a), y + r * Math.sin(a)])
  }
  const color = ok ? (label === '1' ? 'var(--accent)' : 'var(--demo)') : 'var(--alarm)'
  return (
    <g>
      <polygon points={pts.map((p) => p.map((v) => v.toFixed(1)).join(',')).join(' ')} fill={color} fillOpacity={ok ? 0.08 : 0.05}
            stroke={color} strokeOpacity={0.5} strokeDasharray={ok ? undefined : '5 5'} />
      <circle cx={x} cy={y} r={10} fill={color} />
      <text x={x} y={y + 4} textAnchor="middle" fontSize={11} fontWeight={600} fill="#fff">{label}</text>
    </g>
  )
}

function Label({ x, y, children, anchor = 'middle' }: { x: number; y: number; children: ReactNode; anchor?: 'start' | 'middle' | 'end' }) {
  return <text x={x} y={y} textAnchor={anchor} fontSize={11.5} fill="var(--muted)">{children}</text>
}

export function Schematic({ info, snap }: { info: SiteInfo; snap: Snapshot | null }) {
  const title = (g: string) => info.layout?.groups.find((x) => x.id === g)?.title || g
  const sig = (g: string) => snap?.signals[g] || 'red'
  const o = snap?.observation
  const q = (g: string) => (o ? o.queue[g] ?? null : 0)
  const em = (g: string) => !!o?.emergency.includes(g)
  const badge = (g: string, x: number, y: number) => <Badge x={x} y={y} n={o ? o.waiting[g] : 0} wait={o?.max_wait[g]} walking={o?.on_crosswalk[g]} />
  const cam = (id: string) => snap?.cameras.find((c) => c.id.endsWith(id))?.ok ?? true
  const road = 'var(--road)'
  const walk = 'var(--walk)'

  let body: ReactNode
  if (info.kind === 'crossing') {
    body = (
      <>
        <rect x={140} y={0} width={30} height={W} fill={walk} />
        <rect x={270} y={0} width={30} height={W} fill={walk} />
        <rect x={170} y={0} width={100} height={W} fill={road} />
        <line x1={220} y1={0} x2={220} y2={W} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <Zebra x={170} y={200} w={100} h={40} across="v" />
        <rect x={170} y={190} width={50} height={3} fill="var(--zebra)" />
        <rect x={220} y={247} width={50} height={3} fill="var(--zebra)" />
        <Queue x={195} y={190} dir="down" n={q('veh')} emergency={em('veh')} />
        <VehHead x={155} y={170} s={sig('veh')} />
        <VehHead x={285} y={270} s={sig('veh')} />
        <PedHead x={155} y={220} s={sig('ped')} />
        <PedHead x={285} y={220} s={sig('ped')} />
        {badge('ped', 360, 216)}
        <Cam x={300} y={262} angle={-126} ok={cam('cam1')} label="1" />
        <Cam x={140} y={178} angle={54} ok={cam('cam2')} label="2" />
        <Label x={195} y={16}>{title('veh')}</Label>
      </>
    )
  } else if (info.kind === 'tee') {
    body = (
      <>
        <rect x={140} y={0} width={30} height={W} fill={walk} />
        <rect x={270} y={0} width={30} height={160} fill={walk} />
        <rect x={270} y={290} width={30} height={150} fill={walk} />
        <rect x={270} y={160} width={170} height={30} fill={walk} />
        <rect x={270} y={260} width={170} height={30} fill={walk} />
        <rect x={170} y={0} width={100} height={W} fill={road} />
        <rect x={270} y={190} width={170} height={70} fill={road} />
        <line x1={220} y1={0} x2={220} y2={W} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <line x1={270} y1={225} x2={W} y2={225} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <Zebra x={170} y={128} w={100} h={32} across="v" />
        <Zebra x={300} y={190} w={30} h={70} across="h" />
        <rect x={170} y={119} width={50} height={3} fill="var(--zebra)" />
        <rect x={338} y={190} width={3} height={35} fill="var(--zebra)" />
        <Queue x={195} y={118} dir="down" n={q('veh_A')} emergency={em('veh_A')} />
        <Queue x={342} y={207} dir="left" n={q('veh_B')} emergency={em('veh_B')} />
        <VehHead x={155} y={98} s={sig('veh_A')} />
        <VehHead x={285} y={318} s={sig('veh_A')} />
        <VehHead x={372} y={168} s={sig('veh_B')} />
        <PedHead x={155} y={144} s={sig('ped_A')} />
        <PedHead x={285} y={144} s={sig('ped_A')} />
        <PedHead x={315} y={176} s={sig('ped_B')} />
        <PedHead x={315} y={275} s={sig('ped_B')} />
        {badge('ped_A', 360, 132)}
        {badge('ped_B', 360, 318)}
        <Cam x={140} y={225} angle={0} ok={cam('cam1')} label="1" />
        <Cam x={300} y={292} angle={-119} ok={cam('cam2')} label="2" />
        <Label x={195} y={16}>{title('veh_A')}</Label>
        <Label x={432} y={252} anchor="end">{title('veh_B')}</Label>
      </>
    )
  } else {
    body = (
      <>
        <rect x={140} y={0} width={30} height={W} fill={walk} />
        <rect x={270} y={0} width={30} height={W} fill={walk} />
        <rect x={0} y={140} width={W} height={30} fill={walk} />
        <rect x={0} y={270} width={W} height={30} fill={walk} />
        <rect x={170} y={0} width={100} height={W} fill={road} />
        <rect x={0} y={170} width={W} height={100} fill={road} />
        <line x1={220} y1={0} x2={220} y2={125} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <line x1={220} y1={315} x2={220} y2={W} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <line x1={0} y1={220} x2={125} y2={220} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <line x1={315} y1={220} x2={W} y2={220} stroke="var(--zebra)" strokeWidth={2} strokeDasharray="14 10" opacity={0.7} />
        <Zebra x={170} y={128} w={100} h={32} across="v" />
        <Zebra x={170} y={280} w={100} h={32} across="v" />
        <Zebra x={128} y={170} w={32} h={100} across="h" />
        <Zebra x={280} y={170} w={32} h={100} across="h" />
        <rect x={170} y={119} width={50} height={3} fill="var(--zebra)" />
        <rect x={220} y={318} width={50} height={3} fill="var(--zebra)" />
        <rect x={119} y={220} width={3} height={50} fill="var(--zebra)" />
        <rect x={318} y={170} width={3} height={50} fill="var(--zebra)" />
        <Queue x={195} y={118} dir="down" n={q('veh_A')} emergency={em('veh_A')} />
        <Queue x={118} y={245} dir="right" n={q('veh_B')} emergency={em('veh_B')} />
        <VehHead x={155} y={96} s={sig('veh_A')} />
        <VehHead x={285} y={344} s={sig('veh_A')} />
        <VehHead x={96} y={290} s={sig('veh_B')} />
        <VehHead x={344} y={150} s={sig('veh_B')} />
        {['ped_A', 'ped_A'].map((g, i) => (
          <g key={i}>
            <PedHead x={156} y={i ? 296 : 144} s={sig(g)} />
            <PedHead x={284} y={i ? 296 : 144} s={sig(g)} />
          </g>
        ))}
        {['ped_B', 'ped_B'].map((g, i) => (
          <g key={i}>
            <PedHead x={i ? 296 : 144} y={157} s={sig(g)} />
            <PedHead x={i ? 296 : 144} y={283} s={sig(g)} />
          </g>
        ))}
        {badge('ped_A', 262, 100)}
        {badge('ped_B', 70, 196)}
        <Cam x={150} y={150} angle={45} ok={cam('cam1')} label="1" />
        <Cam x={290} y={290} angle={-135} ok={cam('cam2')} label="2" />
        <Label x={195} y={16}>{title('veh_A')}</Label>
        <Label x={8} y={262} anchor="start">{title('veh_B')}</Label>
      </>
    )
  }

  return (
    <svg className="schematic" viewBox={`0 0 ${W} ${W}`} role="img" aria-label={`Схема объекта ${info.title}`}>
      <defs>
        <clipPath id="sch-clip"><rect width={W} height={W} rx={10} /></clipPath>
      </defs>
      <g clipPath="url(#sch-clip)">
        <rect width={W} height={W} fill="var(--block)" />
        {body}
      </g>
    </svg>
  )
}
