"""
Le routes.json e results.json do scraper principal (read-only).
Cache em memoria com TTL de 60s, recarrega quando mtime muda.
"""

import json
import os
import time
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

# /app/data quando rodando em Docker (volume montado),
# senao busca no diretorio padrao do scraper local
def _resolve_data_dir() -> str:
    candidates = [
        "/app/data",                                      # docker (volume mapeado)
        os.path.join(os.path.dirname(__file__), "..", "data"),  # local: ../data
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return os.path.abspath(candidates[1])


DATA_DIR = _resolve_data_dir()
ROUTES_FILE = os.path.join(DATA_DIR, "routes.json")
RESULTS_FILE = os.path.join(DATA_DIR, "results.json")

_CACHE_TTL = 60  # segundos
_cache: Dict[str, Any] = {
    "loaded_at": 0,
    "routes_mtime": 0,
    "results_mtime": 0,
    "routes": [],
    "results": {},
}

# Mapeamento mes pt -> numero (mesmo de search_engine.py)
_MONTH_PT = {
    "Janeiro": 1, "Fevereiro": 2, "Marco": 3, "Abril": 4,
    "Maio": 5, "Junho": 6, "Julho": 7, "Agosto": 8,
    "Setembro": 9, "Outubro": 10, "Novembro": 11, "Dezembro": 12,
}


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def reload_if_stale(force: bool = False) -> None:
    """Recarrega routes.json e results.json se mudaram OU TTL expirou."""
    now = time.time()
    rm = _file_mtime(ROUTES_FILE)
    sm = _file_mtime(RESULTS_FILE)
    expired = (now - _cache["loaded_at"]) > _CACHE_TTL
    changed = rm != _cache["routes_mtime"] or sm != _cache["results_mtime"]

    if not (force or expired or changed):
        return

    routes: List[dict] = []
    results: Dict[str, dict] = {}
    try:
        if os.path.exists(ROUTES_FILE):
            with open(ROUTES_FILE) as f:
                routes = json.load(f)
    except Exception as e:
        print(f"[data_loader] erro lendo routes.json: {e}")
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE) as f:
                results = json.load(f)
    except Exception as e:
        print(f"[data_loader] erro lendo results.json: {e}")

    _cache["routes"] = routes
    _cache["results"] = results
    _cache["routes_mtime"] = rm
    _cache["results_mtime"] = sm
    _cache["loaded_at"] = now


def get_routes() -> List[dict]:
    reload_if_stale()
    return _cache["routes"]


def get_results() -> Dict[str, dict]:
    reload_if_stale()
    return _cache["results"]


def list_origins() -> List[str]:
    """Lista origens distintas das rotas com pelo menos 1 resultado."""
    results = get_results()
    routes = get_routes()
    origins = set()
    for r in routes:
        if r["id"] in results:
            origins.add(r["origin"])
    return sorted(origins)


def list_destinations() -> List[str]:
    results = get_results()
    routes = get_routes()
    dests = set()
    for r in routes:
        if r["id"] in results:
            dests.add(r["dest"])
    return sorted(dests)


def list_programs() -> List[str]:
    results = get_results()
    routes = get_routes()
    progs = set()
    for r in routes:
        if r["id"] in results:
            progs.add(r["program"])
    return sorted(progs)


def list_available_scopes() -> Dict[str, Any]:
    """Sumario de quantas rotas/snapshots cada programa tem."""
    results = get_results()
    routes = get_routes()
    by_program: Dict[str, int] = {}
    for r in routes:
        if r["id"] in results:
            p = r["program"]
            by_program[p] = by_program.get(p, 0) + 1
    return {
        "total_routes": len(routes),
        "total_with_results": len(results),
        "by_program": by_program,
        "origins": list_origins(),
    }


def _flight_date(month_label: str, day_str: str) -> Optional[str]:
    """('Abril 2026', '28') -> '2026-04-28'."""
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


def _expand_route_dates(
    result: Dict[str, Any], direction: str, max_miles: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Extrai lista [{flight_date, miles}, ...] de outbound ou inbound."""
    out = []
    data = result.get(direction) or {}
    for month_label, days in data.items():
        for d in (days or []):
            if not isinstance(d, dict):
                continue
            miles = d.get("price")
            day_str = d.get("day")
            if miles is None or day_str is None:
                continue
            try:
                miles_int = int(round(float(miles)))
            except (ValueError, TypeError):
                continue
            flight_date = _flight_date(month_label, day_str)
            if not flight_date:
                continue
            if max_miles is not None and miles_int > max_miles:
                continue
            out.append({"flight_date": flight_date, "miles": miles_int})
    return sorted(out, key=lambda x: x["flight_date"])


def search_flights(
    origins: Optional[List[str]] = None,
    dests: Optional[List[str]] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    max_miles: Optional[int] = None,
    program: Optional[str] = None,
    cabin: Optional[str] = None,
    direction_filter: str = "outbound",
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Busca DATAS (trechos soltos) que casam com os filtros.
    Por default retorna ida (outbound). Para volta, passe direction_filter='inbound'.

    Cada item: {origin, dest, program, cabin, flight_date, miles, route_id, direction}
    """
    routes = get_routes()
    results = get_results()
    out: List[Dict[str, Any]] = []

    # Normalizar filtros
    origins_set = set(o.upper() for o in origins) if origins else None
    dests_set = set(d.upper() for d in dests) if dests else None

    for route in routes:
        if route["id"] not in results:
            continue
        if origins_set and route["origin"].upper() not in origins_set:
            continue
        if dests_set and route["dest"].upper() not in dests_set:
            continue
        if program and route["program"].upper() != program.upper():
            continue
        if cabin and (route.get("cabin") or "").lower() != cabin.lower():
            continue

        result = results[route["id"]]
        # Para rotas roundtrip o usuario pode querer outbound (ida) ou inbound (volta)
        items = _expand_route_dates(result, direction_filter, max_miles)
        for item in items:
            if date_start and item["flight_date"] < date_start:
                continue
            if date_end and item["flight_date"] > date_end:
                continue
            out.append({
                "route_id": route["id"],
                "origin": route["origin"],
                "dest": route["dest"],
                "program": route["program"],
                "cabin": route.get("cabin", "economy"),
                "direction": direction_filter,
                "flight_date": item["flight_date"],
                "miles": item["miles"],
                "pricing_type": "one_way",  # cada linha eh um trecho
            })

    out.sort(key=lambda x: (x["miles"], x["flight_date"]))
    return out[:limit]


def find_trip_combinations(
    origins: Optional[List[str]] = None,
    dests: Optional[List[str]] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    max_total_miles: Optional[int] = None,
    program: Optional[str] = None,
    cabin: Optional[str] = None,
    min_trip_days: int = 3,
    max_trip_days: int = 30,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Para cada rota roundtrip que casa, gera pares (ida, volta) onde:
      - ida_date <= volta_date
      - min_trip_days <= (volta - ida) <= max_trip_days
      - total_miles <= max_total_miles (se setado)

    Cada item: {route_id, origin, dest, program, cabin, outbound_date,
                inbound_date, outbound_miles, inbound_miles, total_miles,
                trip_days, pricing_type}
    """
    routes = get_routes()
    results = get_results()
    out: List[Dict[str, Any]] = []

    origins_set = set(o.upper() for o in origins) if origins else None
    dests_set = set(d.upper() for d in dests) if dests else None

    for route in routes:
        if route["id"] not in results:
            continue
        if origins_set and route["origin"].upper() not in origins_set:
            continue
        if dests_set and route["dest"].upper() not in dests_set:
            continue
        if program and route["program"].upper() != program.upper():
            continue
        if cabin and (route.get("cabin") or "").lower() != cabin.lower():
            continue

        # So combina rotas que tem outbound E inbound
        if route.get("direction") != "roundtrip":
            continue

        result = results[route["id"]]
        outs = _expand_route_dates(result, "outbound")
        ins = _expand_route_dates(result, "inbound")
        if not outs or not ins:
            continue

        for o in outs:
            o_date = o["flight_date"]
            if date_start and o_date < date_start:
                continue
            if date_end and o_date > date_end:
                continue
            o_dt = datetime.fromisoformat(o_date)

            # busca voltas validas
            for i in ins:
                i_date = i["flight_date"]
                if i_date < o_date:
                    continue
                i_dt = datetime.fromisoformat(i_date)
                trip_days = (i_dt - o_dt).days
                if trip_days < min_trip_days or trip_days > max_trip_days:
                    continue
                total = o["miles"] + i["miles"]
                if max_total_miles is not None and total > max_total_miles:
                    continue
                out.append({
                    "route_id": route["id"],
                    "origin": route["origin"],
                    "dest": route["dest"],
                    "program": route["program"],
                    "cabin": route.get("cabin", "economy"),
                    "outbound_date": o_date,
                    "inbound_date": i_date,
                    "outbound_miles": o["miles"],
                    "inbound_miles": i["miles"],
                    "total_miles": total,
                    "trip_days": trip_days,
                    "pricing_type": "two_one_ways",  # somou 2 trechos one-way
                })

    out.sort(key=lambda x: (x["total_miles"], x["outbound_date"]))
    return out[:limit]


def get_route_details(route_id: str, max_miles: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Detalhe completo de 1 rota: origin, dest, program, lista de outbound
    e inbound em formato achatado [{flight_date, miles}]."""
    routes = get_routes()
    results = get_results()
    route = next((r for r in routes if r["id"] == route_id), None)
    if not route or route_id not in results:
        return None
    result = results[route_id]
    return {
        "route_id": route_id,
        "origin": route["origin"],
        "dest": route["dest"],
        "program": route["program"],
        "cabin": route.get("cabin", "economy"),
        "direction": route.get("direction", "roundtrip"),
        "min_miles": route.get("min_price_k"),
        "last_searched_at": route.get("last_searched_at"),
        "outbound": _expand_route_dates(result, "outbound", max_miles),
        "inbound": _expand_route_dates(result, "inbound", max_miles),
    }
