"""
Persistencia de historico de buscas em SQLite.
Cada vez que uma rota completa com sucesso, um snapshot eh inserido em `snapshots`
+ uma linha em `prices` por dia/direcao com milhas.

Diferente do results.json (que so guarda o ESTADO ATUAL de cada rota),
esta tabela guarda HISTORICO completo - util pra analytics:
top quedas de preco, abaixo da media, evolucao por rota, etc.

Smiles e TAP usam milhas (nao moeda), entao colunas se chamam min_miles/miles.
"""

import asyncio
import os
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

# Mesmo padrao usado em routes_store.py: usa /app/data se existir (Docker volume)
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(__file__)
DB_FILE = os.path.join(_DATA_DIR, "prices.db")

# Mapeamento mes PT-BR -> numero (mesmo do search_engine.py)
_MONTH_PT = {
    "Janeiro": 1, "Fevereiro": 2, "Marco": 3, "Abril": 4,
    "Maio": 5, "Junho": 6, "Julho": 7, "Agosto": 8,
    "Setembro": 9, "Outubro": 10, "Novembro": 11, "Dezembro": 12,
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  route_id    TEXT NOT NULL,
  searched_at TEXT NOT NULL,
  origin      TEXT NOT NULL,
  dest        TEXT NOT NULL,
  program     TEXT NOT NULL,
  cabin       TEXT NOT NULL,
  direction   TEXT NOT NULL,
  min_miles   INTEGER,
  account_id  TEXT
);

CREATE TABLE IF NOT EXISTS prices (
  snapshot_id  INTEGER NOT NULL,
  flight_date  TEXT NOT NULL,
  direction    TEXT NOT NULL,
  miles        INTEGER NOT NULL,
  PRIMARY KEY (snapshot_id, flight_date, direction),
  FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snap_route          ON snapshots(route_id);
CREATE INDEX IF NOT EXISTS idx_snap_searched       ON snapshots(searched_at);
CREATE INDEX IF NOT EXISTS idx_snap_route_searched ON snapshots(route_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_prices_snap         ON prices(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_prices_flight       ON prices(flight_date);
"""


def _parse_searched_at(s: str) -> str:
    """Converte 'DD/MM/YYYY HH:MM' (formato BR usado em results.json)
    para ISO 'YYYY-MM-DDTHH:MM:SS'. Se ja vier ISO, devolve como esta.
    """
    if not s:
        return datetime.now().replace(microsecond=0).isoformat()
    # Tenta formato BR primeiro
    try:
        dt = datetime.strptime(s, "%d/%m/%Y %H:%M")
        return dt.replace(microsecond=0).isoformat()
    except (ValueError, TypeError):
        pass
    # Tenta ISO direto
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(microsecond=0).isoformat()
    except (ValueError, TypeError):
        pass
    # Fallback
    return datetime.now().replace(microsecond=0).isoformat()


def _flight_date_iso(month_label: str, day_str: str) -> Optional[str]:
    """Converte ('Abril 2026', '28') -> '2026-04-28'.
    Retorna None se nao conseguir parsear.
    """
    try:
        parts = month_label.strip().split()
        if len(parts) != 2:
            return None
        mes_nome, ano = parts
        mes_num = _MONTH_PT.get(mes_nome)
        if not mes_num:
            return None
        day = int(day_str)
        return f"{int(ano):04d}-{mes_num:02d}-{day:02d}"
    except (ValueError, AttributeError, TypeError):
        return None


def _flatten_prices(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrai (flight_date, direction, miles) de result.outbound + result.inbound.
    Retorna lista de dicts. Filtra entradas invalidas/sem preco."""
    rows = []
    for direction in ("outbound", "inbound"):
        data = result.get(direction) or {}
        for month_label, days in data.items():
            for d in (days or []):
                if not isinstance(d, dict):
                    continue
                miles = d.get("price")
                day_str = d.get("day")
                if miles is None or day_str is None:
                    continue
                flight_date = _flight_date_iso(month_label, day_str)
                if not flight_date:
                    continue
                try:
                    miles_int = int(round(float(miles)))
                except (ValueError, TypeError):
                    continue
                rows.append({
                    "flight_date": flight_date,
                    "direction": direction,
                    "miles": miles_int,
                })
    return rows


class PriceDatabase:
    """SQLite-backed price history. All write operations are serialized
    via asyncio.Lock since SQLite WAL still benefits from single-writer."""

    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # =================== WRITES ===================

    async def save_snapshot(self, route_id: str, result: Dict[str, Any]) -> Optional[int]:
        """Insere snapshot + linhas de prices. Retorna o snapshot_id criado.
        Se result nao tiver dados validos (zero precos), insere snapshot
        com min_miles=NULL mesmo assim - util pra registrar tentativas."""
        rows = _flatten_prices(result)
        min_miles = min((r["miles"] for r in rows), default=None)
        searched_at_iso = _parse_searched_at(result.get("searched_at", ""))

        async with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO snapshots
                  (route_id, searched_at, origin, dest, program, cabin, direction,
                   min_miles, account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                route_id,
                searched_at_iso,
                (result.get("origin") or "").upper(),
                (result.get("dest") or "").upper(),
                (result.get("program") or "").upper(),
                result.get("cabin") or "economy",
                result.get("direction") or "roundtrip",
                min_miles,
                result.get("account_id"),
            ))
            snapshot_id = cur.lastrowid
            if rows:
                cur.executemany("""
                    INSERT OR REPLACE INTO prices
                      (snapshot_id, flight_date, direction, miles)
                    VALUES (?, ?, ?, ?)
                """, [
                    (snapshot_id, r["flight_date"], r["direction"], r["miles"])
                    for r in rows
                ])
            self._conn.commit()
            return snapshot_id

    # =================== READS ===================

    async def has_any_snapshot(self, route_id: str) -> bool:
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT 1 FROM snapshots WHERE route_id = ? LIMIT 1", (route_id,))
            return cur.fetchone() is not None

    async def get_snapshots(
        self, route_id: str, days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        async with self._lock:
            cur = self._conn.cursor()
            if days:
                cur.execute("""
                    SELECT * FROM snapshots
                    WHERE route_id = ?
                      AND searched_at >= datetime('now', ?)
                    ORDER BY searched_at DESC
                """, (route_id, f"-{int(days)} days"))
            else:
                cur.execute("""
                    SELECT * FROM snapshots
                    WHERE route_id = ?
                    ORDER BY searched_at DESC
                """, (route_id,))
            return [dict(r) for r in cur.fetchall()]

    async def get_snapshot_prices(self, snapshot_id: int) -> Dict[str, Dict[str, int]]:
        """Reconstitui {outbound: {date: miles, ...}, inbound: {...}}
        a partir da tabela prices."""
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT flight_date, direction, miles
                FROM prices WHERE snapshot_id = ?
                ORDER BY flight_date ASC
            """, (snapshot_id,))
            out: Dict[str, Dict[str, int]] = {"outbound": {}, "inbound": {}}
            for r in cur.fetchall():
                out.setdefault(r["direction"], {})[r["flight_date"]] = r["miles"]
            return out

    async def get_trend(self, route_id: str, days: int = 30) -> Dict[str, Any]:
        """Retorna estatisticas da rota nos ultimos N dias.
        - current: ultimo snapshot
        - previous: snapshot anterior ao current
        - avg_30d, min_30d, max_30d: agregados sobre min_miles
        - count_30d: quantos snapshots no periodo
        - delta_vs_previous: current.min_miles - previous.min_miles
        - delta_pct_vs_avg: (current - avg) / avg * 100
        """
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                SELECT id, searched_at, min_miles
                FROM snapshots
                WHERE route_id = ?
                  AND searched_at >= datetime('now', ?)
                ORDER BY searched_at DESC
            """, (route_id, f"-{int(days)} days"))
            rows = cur.fetchall()

        if not rows:
            return {
                "route_id": route_id, "days": days,
                "current": None, "previous": None,
                "avg": None, "min": None, "max": None,
                "count": 0,
                "delta_vs_previous": None,
                "delta_pct_vs_avg": None,
            }

        current = dict(rows[0])
        previous = dict(rows[1]) if len(rows) > 1 else None

        miles_list = [r["min_miles"] for r in rows if r["min_miles"] is not None]
        avg_v = (sum(miles_list) / len(miles_list)) if miles_list else None
        min_v = min(miles_list) if miles_list else None
        max_v = max(miles_list) if miles_list else None

        delta_prev = None
        if previous and current["min_miles"] is not None and previous["min_miles"] is not None:
            delta_prev = current["min_miles"] - previous["min_miles"]

        delta_pct_avg = None
        if avg_v and current["min_miles"] is not None and avg_v > 0:
            delta_pct_avg = round(((current["min_miles"] - avg_v) / avg_v) * 100, 2)

        return {
            "route_id": route_id,
            "days": days,
            "current": current,
            "previous": previous,
            "avg": round(avg_v, 1) if avg_v is not None else None,
            "min": min_v,
            "max": max_v,
            "count": len(rows),
            "delta_vs_previous": delta_prev,
            "delta_pct_vs_avg": delta_pct_avg,
        }
