"""Хранилище центра: минутная статистика, события, параметры объектов (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

from node.runtime import MinuteStats, SiteEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS stats_min (
    site TEXT NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL,
    ped_arrived INTEGER, ped_served INTEGER, wait_sum REAL, wait_max REAL,
    violations INTEGER, groups INTEGER, ped_phases INTEGER,
    veh_passed INTEGER, veh_stopped INTEGER,
    adaptive_s REAL, degraded_s REAL, fixed_s REAL, flashing_s REAL,
    PRIMARY KEY (site, ts, source)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL, ts TEXT NOT NULL, level TEXT, kind TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS events_site_ts ON events(site, ts);
CREATE TABLE IF NOT EXISTS params (site TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zones (site TEXT PRIMARY KEY, json TEXT NOT NULL);
"""

COLS = ["ped_arrived", "ped_served", "wait_sum", "wait_max", "violations", "groups", "ped_phases",
        "veh_passed", "veh_stopped", "adaptive_s", "degraded_s", "fixed_s", "flashing_s"]


def minute_key(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:00")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # ---------- запись ----------
    def add_minute(self, site: str, ts: datetime, m: MinuteStats, source: str) -> None:
        row = [m.ped_arrived, m.ped_served, m.wait_sum, m.wait_max, m.violations, m.groups, m.ped_phases,
               m.veh_passed, m.veh_stopped] + [m.mode_s.get(k, 0.0) for k in ("adaptive", "degraded", "fixed", "flashing")]
        self.add_rows([(site, minute_key(ts), source, *row)])

    def add_rows(self, rows: list[tuple]) -> None:
        q = f"INSERT OR REPLACE INTO stats_min (site, ts, source, {', '.join(COLS)}) VALUES ({', '.join('?' * (3 + len(COLS)))})"
        with self.lock:
            self.conn.executemany(q, rows)
            self.conn.commit()

    def add_events(self, site: str, events: list[SiteEvent]) -> None:
        if not events:
            return
        with self.lock:
            self.conn.executemany("INSERT INTO events (site, ts, level, kind, message) VALUES (?, ?, ?, ?, ?)",
                                  [(site, e.ts.isoformat(timespec="seconds"), e.level, e.kind, e.message) for e in events])
            self.conn.commit()

    def has_source(self, source: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM stats_min WHERE source = ? LIMIT 1", (source,)).fetchone() is not None

    # ---------- параметры ----------
    def get_params(self, site: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT json FROM params WHERE site = ?", (site,)).fetchone()
        return json.loads(r["json"]) if r else None

    def set_params(self, site: str, data: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO params (site, json) VALUES (?, ?)", (site, json.dumps(data)))
            self.conn.commit()

    def get_zones(self, site: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT json FROM zones WHERE site = ?", (site,)).fetchone()
        return json.loads(r["json"]) if r else None

    def set_zones(self, site: str, data: dict | None) -> None:
        with self.lock:
            if data:
                self.conn.execute("INSERT OR REPLACE INTO zones (site, json) VALUES (?, ?)", (site, json.dumps(data)))
            else:
                self.conn.execute("DELETE FROM zones WHERE site = ?", (site,))
            self.conn.commit()

    # ---------- чтение ----------
    def timeseries(self, site: str | None, since: datetime, bucket_min: int) -> list[dict]:
        where, args = ("site = ? AND ", [site]) if site else ("", [])
        q = f"""
        SELECT datetime((CAST(strftime('%s', ts) AS INTEGER) / (? * 60)) * (? * 60), 'unixepoch') AS bucket,
               SUM(ped_arrived) ped_arrived, SUM(ped_served) ped_served, SUM(wait_sum) wait_sum,
               MAX(wait_max) wait_max, SUM(violations) violations, SUM(groups) groups, SUM(ped_phases) ped_phases,
               SUM(veh_passed) veh_passed, SUM(veh_stopped) veh_stopped,
               SUM(CASE WHEN source = 'demo' THEN 1 ELSE 0 END) demo_rows, COUNT(*) rows
        FROM stats_min WHERE {where} ts >= ? GROUP BY bucket ORDER BY bucket"""
        with self.lock:
            rows = self.conn.execute(q, (bucket_min, bucket_min, *args, minute_key(since))).fetchall()
        out = []
        for r in rows:
            served = r["ped_served"] or 0
            veh = r["veh_passed"] or 0
            out.append({
                "t": r["bucket"],
                "ped_served": served,
                "wait_avg": round(r["wait_sum"] / served, 1) if served else None,
                "wait_max": round(r["wait_max"] or 0, 1),
                "violations": r["violations"] or 0,
                "groups": r["groups"] or 0,
                "ped_phases": r["ped_phases"] or 0,
                "veh_passed": veh,
                "veh_flow_vph": round(veh * 60 / bucket_min),
                "stop_share": round((r["veh_stopped"] or 0) / veh, 3) if veh else None,
                "demo": r["demo_rows"] > r["rows"] / 2,
            })
        return out

    def summary(self, site: str | None, since: datetime) -> dict:
        where, args = ("site = ? AND ", [site]) if site else ("", [])
        q = f"""SELECT SUM(ped_served) ped, SUM(wait_sum) ws, MAX(wait_max) wm, SUM(violations) v, SUM(groups) g,
                       SUM(ped_phases) pp, SUM(veh_passed) veh, SUM(veh_stopped) vs,
                       SUM(adaptive_s) a, SUM(degraded_s) d, SUM(fixed_s) f, SUM(flashing_s) fl,
                       SUM(CASE WHEN source = 'demo' THEN 1 ELSE 0 END) demo, COUNT(*) n
                FROM stats_min WHERE {where} ts >= ?"""
        with self.lock:
            r = self.conn.execute(q, (*args, minute_key(since))).fetchone()
        ped, veh = r["ped"] or 0, r["veh"] or 0
        modes = {k: r[c] or 0 for k, c in (("adaptive", "a"), ("degraded", "d"), ("fixed", "f"), ("flashing", "fl"))}
        total_mode = sum(modes.values()) or 1
        return {
            "ped_served": ped,
            "wait_avg": round(r["ws"] / ped, 1) if ped else None,
            "wait_max": round(r["wm"] or 0, 1),
            "violation_share": round((r["v"] or 0) / ped, 4) if ped else None,
            "groups": r["g"] or 0,
            "ped_phases": r["pp"] or 0,
            "veh_passed": veh,
            "stop_share": round((r["vs"] or 0) / veh, 3) if veh else None,
            "mode_share": {k: round(v / total_mode, 4) for k, v in modes.items()},
            "demo_share": round((r["demo"] or 0) / r["n"], 3) if r["n"] else 0,
        }

    def wait_histogram(self, site: str | None, since: datetime) -> list[dict]:
        """Распределение среднего ожидания по минутам (прокси распределения ожидания)."""
        where, args = ("site = ? AND ", [site]) if site else ("", [])
        q = f"""SELECT CAST(wait_sum / ped_served / 10 AS INTEGER) * 10 AS b, SUM(ped_served) n
               FROM stats_min WHERE {where} ts >= ? AND ped_served > 0 GROUP BY b ORDER BY b"""
        with self.lock:
            rows = self.conn.execute(q, (*args, minute_key(since))).fetchall()
        return [{"from_s": r["b"], "count": r["n"]} for r in rows]

    def count_events(self, site: str | None, since: datetime, levels: tuple[str, ...]) -> int:
        cond, args = ["ts >= ?", f"level IN ({', '.join('?' * len(levels))})"], [since.isoformat(timespec="seconds"), *levels]
        if site:
            cond.append("site = ?")
            args.append(site)
        with self.lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM events WHERE {' AND '.join(cond)}", args).fetchone()[0]

    def export_csv(self, site: str | None, since: datetime) -> str:
        where, args = ("site = ? AND ", [site]) if site else ("", [])
        with self.lock:
            rows = self.conn.execute(f"SELECT site, ts, {', '.join(COLS)} FROM stats_min WHERE {where} ts >= ? ORDER BY ts, site",
                                     (*args, minute_key(since))).fetchall()
        head = "объект;минута;" + ";".join(COLS)
        return "\n".join([head] + [";".join(str(v) for v in r) for r in rows]) + "\n"

    def events(self, site: str | None, limit: int = 50, levels: tuple[str, ...] | None = None) -> list[dict]:
        cond, args = [], []
        if site:
            cond.append("site = ?")
            args.append(site)
        if levels:
            cond.append(f"level IN ({', '.join('?' * len(levels))})")
            args.extend(levels)
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        with self.lock:
            rows = self.conn.execute(f"SELECT site, ts, level, kind, message FROM events {where} ORDER BY id DESC LIMIT ?",
                                     (*args, limit)).fetchall()
        return [dict(r) for r in rows]
