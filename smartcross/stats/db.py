"""Statistics storage (SQLite by default, any SQLAlchemy URL works, e.g. PostgreSQL)."""
from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import (JSON, Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine,
                        event, func, select)

metadata = MetaData()

traffic_minute = Table(
    "traffic_minute", metadata,
    Column("id", Integer, primary_key=True),
    Column("minute", DateTime, index=True, nullable=False),
    Column("vehicles", Integer, nullable=False, default=0),
    Column("pedestrians", Integer, nullable=False, default=0),
    Column("flow_vph", Float),            # smoothed flow estimate at the end of the minute
    Column("density_vpkm", Float),
    Column("max_waiting", Integer),       # max pedestrians waiting at once
    Column("by_class", JSON),             # {"car": 10, "bus": 1}
    Column("by_direction", JSON),
)

phase_log = Table(
    "phase_log", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime, index=True, nullable=False),
    Column("phase", String(32), nullable=False),     # phase that ended
    Column("duration_s", Float, nullable=False),
    Column("next_phase", String(32)),
    Column("mode", String(16)),
    Column("reason", Text),
)

ped_service = Table(
    "ped_service", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime, index=True, nullable=False),
    Column("wait_s", Float, nullable=False),
    Column("group_size", Integer, nullable=False),
    Column("called", Integer, nullable=False),       # 1 if there was a pedestrian call
    Column("mode", String(16)),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime, index=True, nullable=False),
    Column("level", String(8), nullable=False),      # info / warn / error
    Column("kind", String(32), nullable=False),
    Column("message", Text, nullable=False),
)


@dataclass
class _MinuteAcc:
    minute: datetime
    vehicles: int = 0
    pedestrians: int = 0
    max_waiting: int = 0
    flow_vph: float = 0.0
    density_vpkm: float = 0.0
    by_class: Counter = field(default_factory=Counter)
    by_direction: Counter = field(default_factory=Counter)


class StatsDB:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            from pathlib import Path
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _wal(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
        metadata.create_all(self.engine)
        self._lock = threading.Lock()
        self._acc: _MinuteAcc | None = None
        self._pending: list[tuple[Table, dict]] = []

    # --------------------------------------------------------------- writing
    def add_traffic(self, now: datetime, vehicles_by_dir: dict[str, int], vehicles_by_class: dict[str, int],
                    pedestrians: int, waiting: int, flow_vph: float, density_vpkm: float) -> None:
        minute = now.replace(second=0, microsecond=0)
        with self._lock:
            if self._acc is not None and self._acc.minute != minute:
                self._flush_minute()
            if self._acc is None:
                self._acc = _MinuteAcc(minute)
            a = self._acc
            a.vehicles += sum(vehicles_by_dir.values())
            a.by_direction.update(vehicles_by_dir)
            a.by_class.update(vehicles_by_class)
            a.pedestrians += pedestrians
            a.max_waiting = max(a.max_waiting, waiting)
            a.flow_vph, a.density_vpkm = flow_vph, density_vpkm

    def _flush_minute(self) -> None:
        a = self._acc
        self._pending.append((traffic_minute, dict(
            minute=a.minute, vehicles=a.vehicles, pedestrians=a.pedestrians, flow_vph=a.flow_vph,
            density_vpkm=a.density_vpkm, max_waiting=a.max_waiting, by_class=dict(a.by_class),
            by_direction=dict(a.by_direction))))
        self._acc = None

    def add_phase(self, ts: datetime, phase: str, duration: float, next_phase: str, mode: str, reason: str) -> None:
        with self._lock:
            self._pending.append((phase_log, dict(ts=ts, phase=phase, duration_s=duration, next_phase=next_phase,
                                                  mode=mode, reason=reason)))

    def add_ped_service(self, ts: datetime, wait: float, group: int, called: bool, mode: str) -> None:
        with self._lock:
            self._pending.append((ped_service, dict(ts=ts, wait_s=wait, group_size=group, called=int(called),
                                                    mode=mode)))

    def add_event(self, ts: datetime, level: str, kind: str, message: str) -> None:
        with self._lock:
            self._pending.append((events, dict(ts=ts, level=level, kind=kind, message=message)))

    def flush(self, force_minute: bool = False) -> None:
        with self._lock:
            if force_minute and self._acc is not None:
                self._flush_minute()
            pending, self._pending = self._pending, []
        if not pending:
            return
        with self.engine.begin() as conn:
            by_table: dict[Table, list[dict]] = {}
            for t, row in pending:
                by_table.setdefault(t, []).append(row)
            for t, rows in by_table.items():
                conn.execute(t.insert(), rows)

    # --------------------------------------------------------------- reading
    def summary(self, since: datetime) -> dict:
        with self.engine.connect() as c:
            tr = c.execute(select(func.coalesce(func.sum(traffic_minute.c.vehicles), 0),
                                  func.coalesce(func.sum(traffic_minute.c.pedestrians), 0),
                                  func.max(traffic_minute.c.flow_vph))
                           .where(traffic_minute.c.minute >= since)).one()
            ps = c.execute(select(func.count(), func.avg(ped_service.c.wait_s), func.max(ped_service.c.wait_s),
                                  func.avg(ped_service.c.group_size))
                           .where(ped_service.c.ts >= since, ped_service.c.called == 1)).one()
            phases = c.execute(select(func.count()).select_from(phase_log)
                               .where(phase_log.c.ts >= since, phase_log.c.phase == "ped_green")).scalar()
            modes = dict(c.execute(select(phase_log.c.mode, func.sum(phase_log.c.duration_s))
                                   .where(phase_log.c.ts >= since).group_by(phase_log.c.mode)).all())
            n_events = dict(c.execute(select(events.c.level, func.count())
                                      .where(events.c.ts >= since).group_by(events.c.level)).all())
        return {
            "vehicles": int(tr[0]), "pedestrians": int(tr[1]), "max_flow_vph": tr[2],
            "ped_calls": ps[0], "avg_wait_s": None if ps[1] is None else round(ps[1], 1),
            "max_wait_s": None if ps[2] is None else round(ps[2], 1),
            "avg_group": None if ps[3] is None else round(ps[3], 1),
            "ped_phases": phases, "mode_seconds": {k: round(v or 0) for k, v in modes.items() if k},
            "events": n_events,
        }

    def timeseries(self, since: datetime, bucket_min: int = 1) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(traffic_minute).where(traffic_minute.c.minute >= since)
                             .order_by(traffic_minute.c.minute)).mappings().all()
            waits = c.execute(select(ped_service.c.ts, ped_service.c.wait_s)
                              .where(ped_service.c.ts >= since, ped_service.c.called == 1)).all()
        buckets: dict[datetime, dict] = {}

        def key(ts: datetime) -> datetime:
            m = (ts.hour * 60 + ts.minute) // bucket_min * bucket_min
            return ts.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)

        for r in rows:
            b = buckets.setdefault(key(r["minute"]), {"vehicles": 0, "pedestrians": 0, "flow_vph": 0.0,
                                                      "density_vpkm": 0.0, "waits": []})
            b["vehicles"] += r["vehicles"]
            b["pedestrians"] += r["pedestrians"]
            b["flow_vph"] = max(b["flow_vph"], r["flow_vph"] or 0)
            b["density_vpkm"] = max(b["density_vpkm"], r["density_vpkm"] or 0)
        for ts, w in waits:
            buckets.setdefault(key(ts), {"vehicles": 0, "pedestrians": 0, "flow_vph": 0.0, "density_vpkm": 0.0,
                                         "waits": []})["waits"].append(w)
        out = []
        for k in sorted(buckets):
            b = buckets.pop(k)
            ws = b.pop("waits")
            out.append({"t": k.isoformat(timespec="minutes"), **b,
                        "avg_wait_s": round(sum(ws) / len(ws), 1) if ws else None})
        return out

    def class_breakdown(self, since: datetime) -> dict[str, int]:
        total: Counter = Counter()
        with self.engine.connect() as c:
            for (bc,) in c.execute(select(traffic_minute.c.by_class).where(traffic_minute.c.minute >= since)):
                total.update(bc if isinstance(bc, dict) else json.loads(bc or "{}"))
        return dict(total)

    def wait_histogram(self, since: datetime, step: int = 10) -> list[dict]:
        with self.engine.connect() as c:
            ws = [w for (w,) in c.execute(select(ped_service.c.wait_s)
                                          .where(ped_service.c.ts >= since, ped_service.c.called == 1))]
        hist = Counter(int(w // step) * step for w in ws)
        return [{"from": k, "to": k + step, "count": hist[k]} for k in sorted(hist)]

    def hourly_flow_profile(self, days: int = 14) -> dict[int, float]:
        """Average smoothed flow by hour of day — used as fallback when vehicle cameras are down."""
        since = datetime.now() - timedelta(days=days)
        with self.engine.connect() as c:
            rows = c.execute(select(traffic_minute.c.minute, traffic_minute.c.vehicles)
                             .where(traffic_minute.c.minute >= since)).all()
        per_hour: dict[int, list[int]] = {}
        for minute, v in rows:
            per_hour.setdefault(minute.hour, []).append(v)
        return {h: sum(v) / len(v) * 60 for h, v in per_hour.items()}  # veh/min -> veh/h

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(events).order_by(events.c.id.desc()).limit(limit)).mappings().all()
        return [{**r, "ts": r["ts"].isoformat(timespec="seconds")} for r in rows]

    def recent_phases(self, limit: int = 30) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(phase_log).order_by(phase_log.c.id.desc()).limit(limit)).mappings().all()
        return [{**r, "ts": r["ts"].isoformat(timespec="seconds")} for r in rows]

    def export_csv(self, since: datetime) -> str:
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["minute", "vehicles", "pedestrians", "flow_vph", "density_vpkm", "max_waiting", "by_class"])
        with self.engine.connect() as c:
            for r in c.execute(select(traffic_minute).where(traffic_minute.c.minute >= since)
                               .order_by(traffic_minute.c.minute)).mappings():
                w.writerow([r["minute"].isoformat(), r["vehicles"], r["pedestrians"], r["flow_vph"],
                            r["density_vpkm"], r["max_waiting"], json.dumps(r["by_class"], ensure_ascii=False)])
        return buf.getvalue()
