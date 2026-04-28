"""
Queries de analytics sobre prices.db (read-only).

3 visoes principais:
  - top_drops: rotas com maior queda vs snapshot anterior
  - most_below_average: rotas mais abaixo da media historica
  - cheapest_now: ranking absoluto pelo ultimo snapshot

+ route_history: serie temporal de uma rota especifica
+ list_origins: lista de origens IATA (pra dropdown)

Cache em memoria com TTL 300s pras agregadas, 60s pro history.

Como o writer (database.PriceDatabase) usa WAL, esta conexao read-only
nao briga com ele. Cada chamada abre uma conexao pra evitar problemas
de threading com FastAPI.
"""

import os
import sqlite3
import time
import urllib.parse
from typing import Optional, List, Dict, Any, Callable

# Mesmo padrao do database.py: /app/data se Docker, senao local
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(__file__)
DB_FILE = os.path.join(_DATA_DIR, "prices.db")

# Cache simples em memoria
_CACHE: Dict[str, "tuple[float, Any]"] = {}
_CACHE_TTL_DEFAULT = 300   # 5 min
_CACHE_TTL_HISTORY = 60    # 1 min


def _conn() -> sqlite3.Connection:
    """Conexao read-only via URI - nao bloqueia o writer (WAL)."""
    # URL-encode no path pra suportar caminhos com espacos / caracteres especiais
    encoded_path = urllib.parse.quote(DB_FILE, safe="/")
    con = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _cached(key: str, fn: Callable[[], Any], ttl: int = _CACHE_TTL_DEFAULT) -> Any:
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts < ttl:
            return val
    val = fn()
    _CACHE[key] = (now, val)
    return val


def invalidate_cache() -> None:
    _CACHE.clear()


def _build_filters(program: str = "all", cabin: str = "all") -> "tuple[str, list]":
    """Helper: monta cl\u00e1usula WHERE e parametros pros filtros opcionais.
    Retorna (where_clause, params). Cl\u00e1usula come\u00e7a com 'AND' se houver
    algo, ou string vazia."""
    clauses = []
    params: list = []
    if program and program != "all":
        clauses.append("program = ?")
        params.append(program.upper())
    if cabin and cabin != "all":
        clauses.append("cabin = ?")
        params.append(cabin.lower())
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ===================== queries =====================

def list_origins() -> List[str]:
    def fn():
        with _conn() as con:
            cur = con.cursor()
            cur.execute("SELECT DISTINCT origin FROM snapshots ORDER BY origin ASC")
            return [r["origin"] for r in cur.fetchall()]
    return _cached("origins", fn, ttl=_CACHE_TTL_DEFAULT)


def list_programs() -> List[str]:
    def fn():
        with _conn() as con:
            cur = con.cursor()
            cur.execute("SELECT DISTINCT program FROM snapshots ORDER BY program ASC")
            return [r["program"] for r in cur.fetchall()]
    return _cached("programs", fn, ttl=_CACHE_TTL_DEFAULT)


def list_cabins() -> List[str]:
    def fn():
        with _conn() as con:
            cur = con.cursor()
            cur.execute("SELECT DISTINCT cabin FROM snapshots ORDER BY cabin ASC")
            return [r["cabin"] for r in cur.fetchall()]
    return _cached("cabins", fn, ttl=_CACHE_TTL_DEFAULT)


def top_drops(
    program: str = "all",
    cabin: str = "all",
    limit: int = 10,
    min_drop_pct: float = 0.0,  # negative = caiu mais que isso
    origin: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Maior queda vs snapshot anterior.
    delta_pct = (current - previous) / previous * 100 (negativo = caiu).
    Filtra delta_pct <= min_drop_pct."""
    cache_key = f"drops:{program}:{cabin}:{limit}:{min_drop_pct}:{origin}"

    def fn():
        where, params = _build_filters(program, cabin)
        sql_limit = 500 if origin else limit * 3  # eleva pra ter espaco apos filtro python
        sql = f"""
            WITH ranked AS (
                SELECT
                    s.id, s.route_id, s.searched_at, s.origin, s.dest,
                    s.program, s.cabin, s.direction, s.min_miles,
                    ROW_NUMBER() OVER (PARTITION BY s.route_id ORDER BY s.searched_at DESC) AS rn
                FROM snapshots s
                WHERE s.min_miles IS NOT NULL{where}
            ),
            paired AS (
                SELECT
                    cur.route_id,
                    cur.id AS current_id,
                    cur.searched_at AS current_searched_at,
                    cur.origin, cur.dest, cur.program, cur.cabin, cur.direction,
                    cur.min_miles AS current_miles,
                    prev.id AS previous_id,
                    prev.searched_at AS previous_searched_at,
                    prev.min_miles AS previous_miles
                FROM ranked cur
                JOIN ranked prev
                  ON prev.route_id = cur.route_id AND prev.rn = 2
                WHERE cur.rn = 1
                  AND prev.min_miles > 0
            )
            SELECT *,
                CAST(current_miles AS REAL) - previous_miles AS delta_miles,
                ROUND(((CAST(current_miles AS REAL) - previous_miles) / previous_miles) * 100, 2) AS delta_pct
            FROM paired
            WHERE ((CAST(current_miles AS REAL) - previous_miles) / previous_miles) * 100 <= ?
            ORDER BY delta_pct ASC
            LIMIT ?
        """
        with _conn() as con:
            cur = con.cursor()
            cur.execute(sql, [*params, min_drop_pct, sql_limit])
            rows = [dict(r) for r in cur.fetchall()]

        if origin:
            rows = [r for r in rows if r["origin"] == origin.upper()]
        return rows[:limit]

    return _cached(cache_key, fn)


def most_below_average(
    program: str = "all",
    cabin: str = "all",
    limit: int = 10,
    days: int = 30,
    min_pct_below: float = 0.0,  # negative = abaixo da media
    origin: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Mais abaixo da media dos ultimos N dias.
    pct_vs_avg = (current - avg) / avg * 100. Filtra <= min_pct_below."""
    cache_key = f"below:{program}:{cabin}:{limit}:{days}:{min_pct_below}:{origin}"

    def fn():
        where, params = _build_filters(program, cabin)
        sql_limit = 500 if origin else limit * 3
        sql = f"""
            WITH window_data AS (
                SELECT route_id, min_miles
                FROM snapshots
                WHERE min_miles IS NOT NULL{where}
                  AND searched_at >= datetime('now', ?)
            ),
            stats AS (
                SELECT
                    route_id,
                    AVG(min_miles) AS avg_miles,
                    MIN(min_miles) AS min_miles_window,
                    MAX(min_miles) AS max_miles_window,
                    COUNT(*) AS count_snapshots
                FROM window_data
                GROUP BY route_id
                HAVING count_snapshots >= 2
            ),
            current AS (
                SELECT
                    s.route_id, s.id AS current_id, s.searched_at AS current_searched_at,
                    s.origin, s.dest, s.program, s.cabin, s.direction,
                    s.min_miles AS current_miles,
                    ROW_NUMBER() OVER (PARTITION BY s.route_id ORDER BY s.searched_at DESC) AS rn
                FROM snapshots s
                WHERE s.min_miles IS NOT NULL{where}
            )
            SELECT
                stats.route_id, stats.avg_miles, stats.min_miles_window,
                stats.max_miles_window, stats.count_snapshots,
                current.current_id, current.current_searched_at,
                current.origin, current.dest, current.program, current.cabin,
                current.direction, current.current_miles,
                ROUND(((current.current_miles - stats.avg_miles) / stats.avg_miles) * 100, 2) AS pct_vs_avg
            FROM stats
            JOIN current ON current.route_id = stats.route_id AND current.rn = 1
            WHERE ((current.current_miles - stats.avg_miles) / stats.avg_miles) * 100 <= ?
            ORDER BY pct_vs_avg ASC
            LIMIT ?
        """
        # parametros: (where do window_data) + days + (where do current) + min_pct_below + sql_limit
        with _conn() as con:
            cur = con.cursor()
            cur.execute(sql, [*params, f"-{int(days)} days", *params, min_pct_below, sql_limit])
            rows = [dict(r) for r in cur.fetchall()]

        if origin:
            rows = [r for r in rows if r["origin"] == origin.upper()]
        return rows[:limit]

    return _cached(cache_key, fn)


def cheapest_now(
    program: str = "all",
    cabin: str = "all",
    limit: int = 10,
    origin: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Ranking absoluto pelo ultimo snapshot de cada rota (ordenado por menor min_miles)."""
    cache_key = f"cheapest:{program}:{cabin}:{limit}:{origin}"

    def fn():
        where, params = _build_filters(program, cabin)
        sql_limit = 500 if origin else limit * 3
        sql = f"""
            WITH ranked AS (
                SELECT
                    s.id, s.route_id, s.searched_at, s.origin, s.dest,
                    s.program, s.cabin, s.direction, s.min_miles, s.account_id,
                    ROW_NUMBER() OVER (PARTITION BY s.route_id ORDER BY s.searched_at DESC) AS rn
                FROM snapshots s
                WHERE s.min_miles IS NOT NULL{where}
            )
            SELECT
                id AS current_id, route_id, searched_at AS current_searched_at,
                origin, dest, program, cabin, direction,
                min_miles AS current_miles, account_id
            FROM ranked
            WHERE rn = 1
            ORDER BY current_miles ASC
            LIMIT ?
        """
        with _conn() as con:
            cur = con.cursor()
            cur.execute(sql, [*params, sql_limit])
            rows = [dict(r) for r in cur.fetchall()]

        if origin:
            rows = [r for r in rows if r["origin"] == origin.upper()]
        return rows[:limit]

    return _cached(cache_key, fn)


def route_history(route_id: str, days: int = 30) -> Dict[str, Any]:
    """Pontos {searched_at, min_miles} + stats (count/avg/min/max/current/first/pct_vs_avg)."""
    cache_key = f"history:{route_id}:{days}"

    def fn():
        with _conn() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, searched_at, origin, dest, program, cabin, direction,
                       min_miles, account_id
                FROM snapshots
                WHERE route_id = ?
                  AND min_miles IS NOT NULL
                  AND searched_at >= datetime('now', ?)
                ORDER BY searched_at ASC
            """, (route_id, f"-{int(days)} days"))
            rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            return {
                "route_id": route_id, "days": days,
                "points": [],
                "stats": {
                    "count": 0, "avg": None, "min": None, "max": None,
                    "current": None, "first": None, "pct_vs_avg": None,
                    "delta_vs_first": None,
                },
                "meta": None,
            }

        points = [
            {"searched_at": r["searched_at"], "min_miles": r["min_miles"]}
            for r in rows
        ]
        miles_list = [r["min_miles"] for r in rows]
        first_v = miles_list[0]
        current_v = miles_list[-1]
        avg_v = sum(miles_list) / len(miles_list)
        min_v = min(miles_list)
        max_v = max(miles_list)
        pct_vs_avg = round(((current_v - avg_v) / avg_v) * 100, 2) if avg_v > 0 else None
        delta_first = current_v - first_v

        meta = {
            "origin": rows[-1]["origin"],
            "dest": rows[-1]["dest"],
            "program": rows[-1]["program"],
            "cabin": rows[-1]["cabin"],
            "direction": rows[-1]["direction"],
        }

        return {
            "route_id": route_id, "days": days,
            "points": points,
            "stats": {
                "count": len(rows),
                "avg": round(avg_v, 1),
                "min": min_v, "max": max_v,
                "current": current_v, "first": first_v,
                "pct_vs_avg": pct_vs_avg,
                "delta_vs_first": delta_first,
            },
            "meta": meta,
        }

    return _cached(cache_key, fn, ttl=_CACHE_TTL_HISTORY)
