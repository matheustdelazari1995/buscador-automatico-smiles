# AwardTool Scraper - Documentacao Completa para Desenvolvedor

> **Projeto**: Sistema de busca automatizada de disponibilidade de milhas aereas via AwardTool
> **Data**: Abril 2026
> **Stack**: Python 3.11 + FastAPI + Playwright + WebSocket
> **Objetivo final**: Escalar para 20 contas AwardTool rodando em VPS

---

## 1. VISAO GERAL

O sistema faz scraping do site [awardtool.com](https://www.awardtool.com) para encontrar datas com disponibilidade de passagens aereas por milhas baratas. Ele:

1. Abre o AwardTool no Chrome real (nao Chromium de teste) via Playwright
2. Navega mes a mes buscando disponibilidade de uma rota
3. Extrai precos por classe (Economica, Executiva, Primeira) via regex no texto da pagina
4. Filtra datas abaixo do limite de milhas definido pelo usuario
5. Exibe resultados em dashboard web com progresso em tempo real (WebSocket)
6. Envia resumo via WhatsApp (Evolution API)

### Filtros disponiveis no dashboard:
- **Origem / Destino** (codigos IATA)
- **Programa** (TAP Miles&Go ou Smiles GOL)
- **Classe** (Economica, Executiva, Primeira Classe)
- **Max Milhas (K)** (limite maximo por trecho)
- **Direcao** (Ida e volta, So ida, So volta)
- **Meses** (Ano todo ou selecao especifica)
- **WhatsApp** (toggle para enviar ou nao)

---

## 2. ARQUITETURA

```
awardtool-scraper/
|-- server.py              # FastAPI backend (API + WebSocket + queue)
|-- search_engine.py       # Motor de busca (Playwright + extracao JS)
|-- static/
|   |-- index.html         # Dashboard frontend (HTML/CSS/JS single file)
|-- config.json            # Configuracoes (Evolution API + rotas agendadas)
|-- requirements.txt       # Dependencias Python
|-- setup.sh               # Script de setup inicial
|-- Dockerfile             # Docker para VPS
|-- docker-compose.yml     # Orquestracao Docker
|-- .env.example           # Variaveis de ambiente modelo
|-- accounts.example.json  # Modelo para multiplas contas
|-- .gitignore
|-- .browser-profile/      # Perfil persistente do Chrome (cookies/login)
|-- venv/                  # Virtual environment Python
```

### Fluxo de dados:
```
[Frontend HTML] --> POST /api/search --> [FastAPI Queue] --> [Playwright Chrome]
                                              |                      |
                                         WebSocket <-- progress  --> AwardTool.com
                                              |                      |
                                         WebSocket <-- resultado --> Regex extrai precos
                                              |
                                    [Evolution API] --> WhatsApp
```

---

## 3. ARQUIVOS - CODIGO COMPLETO

### 3.1 search_engine.py (Motor de busca)

```python
"""
Motor de busca AwardTool via Playwright.
Extrai disponibilidade de milhas mes a mes e retorna datas baratas.
"""

import asyncio
import json
import re
import os
import math
from datetime import datetime
from calendar import monthrange
from playwright.async_api import async_playwright


MONTH_NAMES_PT = [
    "", "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]

DELAY_BETWEEN_SEARCHES = 20  # seconds between page loads


def get_months_to_search():
    """Returns list of (year, month, name, start_ts, end_ts) for next 12 months."""
    today = datetime.now()
    start_month = today.month + (1 if today.day > 15 else 0)
    start_year = today.year
    if start_month > 12:
        start_month -= 12
        start_year += 1

    months = []
    for i in range(12):
        m = start_month + i
        y = start_year
        while m > 12:
            m -= 12
            y += 1
        start_ts = int(datetime(y, m, 1).timestamp())
        _, last_day = monthrange(y, m)
        end_ts = int(datetime(y, m, last_day, 23, 59, 59).timestamp())
        name = f"{MONTH_NAMES_PT[m]} {y}"
        months.append({"year": y, "month": m, "name": name, "start": start_ts, "end": end_ts})
    return months


def build_url(origin, dest, program, start_ts, end_ts):
    return (
        f"https://www.awardtool.com/flight?flightWay=oneway&pax=1&children=0"
        f"&cabins=Economy%26Premium+Economy%26Business%26First"
        f"&range=true&rangeV2=false"
        f"&from={origin}&to={dest}&programs={program}&targetId="
        f"&oneWayRangeStartDate={start_ts}&oneWayRangeEndDate={end_ts}"
    )


CABIN_MATCH_INDEX = {
    "economy": 2,
    "business": 4,
    "first": 5,
}

JS_EXTRACT_TEMPLATE = """
() => {{
    const text = document.body.innerText;
    const regex = /((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\\s+\\d{{2}}\\/\\d{{2}})\\nOnly\\n([^\\n]+)\\n([^\\n]+)\\n([^\\n]+)\\n([^\\n]+)/g;
    const results = [];
    let match;
    while ((match = regex.exec(text)) !== null) {{
        const cabin = match[{cabin_index}].trim();
        const m = cabin.match(/([\\.\\d]+)K/);
        if (m && parseFloat(m[1]) <= {max_price}) {{
            results.push({{
                day: match[1].trim().split(' ')[1].split('/')[1],
                price: parseFloat(m[1])
            }});
        }}
    }}
    return {{ count: results.length, days: results }};
}}
"""


class AwardToolSearchEngine:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self._started = False

    async def start(self):
        if self._started:
            return
        browser_profile = os.path.join(os.path.dirname(__file__), ".browser-profile")
        self.playwright = await async_playwright().__aenter__()
        # IMPORTANTE: Usa Chrome real do sistema (nao Chromium de teste)
        # Isso evita deteccao de bot pelo AwardTool
        self.context = await self.playwright.chromium.launch_persistent_context(
            browser_profile,
            channel="chrome",
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self._started = True
        print("[Engine] Chrome real do sistema iniciado")

    async def stop(self):
        if self.context:
            await self.context.close()
        self._started = False

    async def _search_direction(self, origin, dest, program, max_price_k, months, progress_cb=None, cabin="economy"):
        """Search one direction (e.g. VIX->AEP) for all months."""
        cabin_index = CABIN_MATCH_INDEX.get(cabin, 2)
        results = {}
        for i, month in enumerate(months):
            url = build_url(origin, dest, program, month["start"], month["end"])
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(DELAY_BETWEEN_SEARCHES)

                js = JS_EXTRACT_TEMPLATE.format(max_price=max_price_k, cabin_index=cabin_index)
                data = await self.page.evaluate(js)

                if data["count"] > 0:
                    results[month["name"]] = data["days"]

                if progress_cb:
                    await progress_cb(i + 1, len(months), month["name"], data["count"])

            except Exception as e:
                if progress_cb:
                    await progress_cb(i + 1, len(months), month["name"], -1, str(e))

        return results

    async def search_route(self, origin, dest, program, max_price_k, progress_cb=None, selected_months=None, direction="roundtrip", cabin="economy"):
        """
        Full search: outbound + return for a route.
        selected_months: None = all 12 months, or list of {"year": int, "month": int}
        direction: "roundtrip" (ida+volta), "outbound" (so ida), "inbound" (so volta)
        cabin: "economy" or "business" or "first"
        progress_cb: async fn(step, total_steps, detail_msg)
        """
        if not self._started:
            await self.start()

        all_months = get_months_to_search()

        if selected_months:
            selected_set = set()
            for sm in selected_months:
                if isinstance(sm, dict):
                    selected_set.add((sm["year"], sm["month"]))
                else:
                    selected_set.add((sm.year, sm.month))
            months = [m for m in all_months if (m["year"], m["month"]) in selected_set]
        else:
            months = all_months

        do_outbound = direction in ("roundtrip", "outbound")
        do_inbound = direction in ("roundtrip", "inbound")
        total_steps = len(months) * (int(do_outbound) + int(do_inbound))
        offset = len(months) if do_outbound else 0

        outbound = {}
        inbound = {}

        if do_outbound:
            async def ida_progress(i, total, month_name, count, error=None):
                if progress_cb:
                    msg = f"IDA {origin}->{dest}: {month_name}"
                    if error:
                        msg += f" (erro: {error})"
                    elif count >= 0:
                        msg += f" ({count} datas)"
                    await progress_cb(i, total_steps, msg)

            outbound = await self._search_direction(origin, dest, program, max_price_k, months, ida_progress, cabin=cabin)

        if do_inbound:
            async def volta_progress(i, total, month_name, count, error=None):
                if progress_cb:
                    msg = f"VOLTA {dest}->{origin}: {month_name}"
                    if error:
                        msg += f" (erro: {error})"
                    elif count >= 0:
                        msg += f" ({count} datas)"
                    await progress_cb(offset + i, total_steps, msg)

            inbound = await self._search_direction(dest, origin, program, max_price_k, months, volta_progress, cabin=cabin)

        return {
            "origin": origin,
            "dest": dest,
            "program": program,
            "max_price_k": max_price_k,
            "direction": direction,
            "cabin": cabin,
            "outbound": outbound,
            "inbound": inbound,
            "searched_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }


# === Mapeamento IATA -> Cidade ===
IATA_TO_CITY = {
    "GRU": "Sao Paulo", "CGH": "Sao Paulo", "VCP": "Campinas",
    "GIG": "Rio de Janeiro", "SDU": "Rio de Janeiro",
    "BSB": "Brasilia", "CNF": "Belo Horizonte", "SSA": "Salvador",
    "REC": "Recife", "FOR": "Fortaleza", "POA": "Porto Alegre",
    "CWB": "Curitiba", "BEL": "Belem", "MAO": "Manaus",
    "NAT": "Natal", "FLN": "Florianopolis", "VIX": "Vitoria",
    "GYN": "Goiania", "PMW": "Palmas", "AJU": "Aracaju",
    "THE": "Teresina", "CGR": "Campo Grande", "IGU": "Foz do Iguacu",
    "MCZ": "Maceio", "SLZ": "Sao Luis", "CGB": "Cuiaba",
    "JPA": "Joao Pessoa", "PVH": "Porto Velho", "MCP": "Macapa",
    "BVB": "Boa Vista", "RBR": "Rio Branco",
    "LIS": "Lisboa", "OPO": "Porto", "FAO": "Faro",
    "CDG": "Paris", "ORY": "Paris", "FCO": "Roma", "MXP": "Milao",
    "MAD": "Madri", "BCN": "Barcelona", "LHR": "Londres",
    "AMS": "Amsterda", "FRA": "Frankfurt", "MUC": "Munique",
    "ZRH": "Zurique", "BRU": "Bruxelas", "DUB": "Dublin",
    "ATH": "Atenas", "IST": "Istambul",
    "MIA": "Miami", "JFK": "Nova York", "EWR": "Nova York",
    "MCO": "Orlando", "LAX": "Los Angeles", "DFW": "Dallas",
    "ATL": "Atlanta", "IAH": "Houston", "ORD": "Chicago",
    "YYZ": "Toronto", "MEX": "Cidade do Mexico",
    "AEP": "Buenos Aires", "EZE": "Buenos Aires",
    "SCL": "Santiago", "BOG": "Bogota", "LIM": "Lima",
    "PTY": "Cidade do Panama", "MVD": "Montevideu",
    "PUJ": "Punta Cana", "CUN": "Cancun", "SJO": "San Jose",
    "HAV": "Havana", "CCS": "Caracas",
    "DXB": "Dubai", "DOH": "Doha", "NRT": "Toquio",
    "HND": "Toquio", "JNB": "Joanesburgo", "CPT": "Cidade do Cabo",
}

BRAZIL_IATA = {
    "GRU", "CGH", "VCP", "GIG", "SDU", "BSB", "CNF", "SSA", "REC", "FOR",
    "POA", "CWB", "BEL", "MAO", "NAT", "FLN", "VIX", "GYN", "PMW", "AJU",
    "THE", "CGR", "IGU", "MCZ", "SLZ", "CGB", "JPA", "PVH", "MCP", "BVB", "RBR",
}

PROGRAM_NAMES = {
    "TP": "TAP Miles&Go",
    "G3": "Smiles GOL",
    "AD": "Azul Fidelidade",
    "LA": "LATAM Pass",
    "AA": "AAdvantage",
    "UA": "United MileagePlus",
    "DL": "Delta SkyMiles",
}


def _extract_days_and_cheapest(direction_data):
    """
    From direction data (dict of month -> list of {day, price} or list of strings),
    returns (formatted_lines, cheapest_price, cheapest_lines).
    """
    all_lines = []
    cheapest_price = None
    cheapest_by_month = {}

    for month_name, days in direction_data.items():
        if days and isinstance(days[0], dict):
            day_strs = [d["day"] for d in days]
            all_lines.append(f"{month_name}: {', '.join(day_strs)}")

            for d in days:
                price = d["price"]
                if cheapest_price is None or price < cheapest_price:
                    cheapest_price = price
                    cheapest_by_month = {}
                if price == cheapest_price:
                    if month_name not in cheapest_by_month:
                        cheapest_by_month[month_name] = []
                    cheapest_by_month[month_name].append(d["day"])
        else:
            all_lines.append(f"{month_name}: {', '.join(days)}")

    cheapest_lines = []
    if cheapest_price is not None and cheapest_by_month:
        for month_name, days in cheapest_by_month.items():
            cheapest_lines.append(f"{month_name}: {', '.join(days)}")

    return all_lines, cheapest_price, cheapest_lines


def format_result_text(result):
    """Format search result for WhatsApp message."""
    origin = result["origin"]
    dest = result["dest"]
    program = result["program"]
    max_k = result["max_price_k"]

    origin_city = IATA_TO_CITY.get(origin, origin)
    dest_city = IATA_TO_CITY.get(dest, dest)
    program_name = PROGRAM_NAMES.get(program, program)

    is_national = origin in BRAZIL_IATA and dest in BRAZIL_IATA
    scope = "Nacional" if is_national else "Internacional"

    ida_lines, ida_cheapest_price, ida_cheapest = _extract_days_and_cheapest(result.get("outbound", {}))
    volta_lines, volta_cheapest_price, volta_cheapest = _extract_days_and_cheapest(result.get("inbound", {}))

    min_price = None
    if ida_cheapest_price is not None:
        min_price = ida_cheapest_price
    if volta_cheapest_price is not None:
        if min_price is None or volta_cheapest_price < min_price:
            min_price = volta_cheapest_price

    lines = []
    lines.append(f"Oportunidade de resgate - {scope}")
    lines.append(f"Programa de fidelidade: {program_name}")
    cabin = result.get("cabin", "economy")
    cabin_names = {"economy": "Economica", "business": "Executiva", "first": "Primeira Classe"}
    lines.append(f"Classe: {cabin_names.get(cabin, 'Economica')}")
    lines.append("")
    lines.append(f"Origem: {origin_city} ({origin})")
    lines.append(f"Destino: {dest_city} ({dest})")
    lines.append("")
    lines.append(f"Quantidade de milhas: a partir de {max_k} mil milhas o trecho")
    lines.append("")

    direction = result.get("direction", "roundtrip")

    if direction in ("roundtrip", "outbound"):
        lines.append("Datas de ida:")
        if ida_lines:
            for l in ida_lines:
                lines.append(l)
        else:
            lines.append("Nenhuma data encontrada")

    if direction in ("roundtrip", "inbound"):
        if direction == "roundtrip":
            lines.append("")
        lines.append("Datas de volta:")
        if volta_lines:
            for l in volta_lines:
                lines.append(l)
        else:
            lines.append("Nenhuma data encontrada")

    return "\n".join(lines)


async def send_whatsapp(text, config):
    """Send message via Evolution API."""
    import aiohttp
    url = f"{config['url']}/message/sendText/{config['instance']}"
    headers = {"Content-Type": "application/json", "apikey": config["api_key"]}
    payload = {"number": config["destination"], "text": text}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            return resp.status == 200 or resp.status == 201
```

### 3.2 server.py (Backend FastAPI)

```python
"""
FastAPI server for AwardTool Miles Dashboard.
Provides REST API + WebSocket for real-time search progress.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from search_engine import (
    AwardToolSearchEngine,
    format_result_text,
    send_whatsapp,
)

app = FastAPI(title="AwardTool Miles Dashboard")

# State (queue created in startup to be on the right event loop)
search_queue = None
active_connections = []
search_results = []
current_search = None
queue_items = []
engine = None

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


# Models
class MonthSelection(BaseModel):
    year: int
    month: int

class SearchRequest(BaseModel):
    origin: str
    dest: str
    program: str
    max_price_k: int
    send_whatsapp: bool = True
    direction: str = "roundtrip"  # "roundtrip", "outbound", "inbound"
    cabin: str = "economy"  # "economy", "business", "first"
    months: Optional[List[dict]] = None  # None = all 12 months, or list of {year, month}


# WebSocket broadcast
async def broadcast(msg: dict):
    dead = []
    for ws in active_connections:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_connections.remove(ws)


# Search worker - processes queue one at a time
async def search_worker():
    global engine, current_search

    engine = AwardToolSearchEngine()

    while True:
        item = await search_queue.get()
        current_search = item

        await broadcast({
            "type": "search_started",
            "search": {
                "id": item["id"],
                "origin": item["origin"],
                "dest": item["dest"],
                "program": item["program"],
                "max_price_k": item["max_price_k"],
            },
        })

        async def progress_cb(step, total, msg):
            pct = round((step / total) * 100)
            await broadcast({
                "type": "progress",
                "search_id": item["id"],
                "step": step,
                "total": total,
                "percent": pct,
                "message": msg,
            })

        try:
            result = await engine.search_route(
                item["origin"],
                item["dest"],
                item["program"],
                item["max_price_k"],
                progress_cb=progress_cb,
                selected_months=item.get("months"),
                direction=item.get("direction", "roundtrip"),
                cabin=item.get("cabin", "economy"),
            )
            result["id"] = item["id"]
            search_results.append(result)

            # Send WhatsApp if enabled
            whatsapp_sent = False
            if item.get("send_whatsapp"):
                config = load_config()
                evo = config.get("evolution_api")
                if evo:
                    text = format_result_text(result)
                    whatsapp_sent = await send_whatsapp(text, evo)

            await broadcast({
                "type": "search_completed",
                "result": result,
                "whatsapp_sent": whatsapp_sent,
            })

        except Exception as e:
            await broadcast({
                "type": "search_error",
                "search_id": item["id"],
                "error": str(e),
            })

        finally:
            current_search = None
            if item["id"] in [q["id"] for q in queue_items]:
                queue_items[:] = [q for q in queue_items if q["id"] != item["id"]]

        search_queue.task_done()


# IMPORTANTE: Queue criada no startup para estar no event loop correto
@app.on_event("startup")
async def startup():
    global search_queue
    search_queue = asyncio.Queue()
    asyncio.create_task(search_worker())


# Routes
@app.post("/api/search")
async def add_search(req: SearchRequest):
    item = {
        "id": str(uuid.uuid4())[:8],
        "origin": req.origin.upper().strip(),
        "dest": req.dest.upper().strip(),
        "program": req.program.upper().strip(),
        "max_price_k": req.max_price_k,
        "send_whatsapp": req.send_whatsapp,
        "direction": req.direction,
        "cabin": req.cabin,
        "months": req.months,
        "queued_at": datetime.now().isoformat(),
    }
    queue_items.append(item)
    await search_queue.put(item)
    await broadcast({"type": "queued", "search": item})
    return {"status": "queued", "search": item}


@app.get("/api/queue")
async def get_queue():
    return {
        "current": current_search,
        "pending": queue_items,
        "queue_size": search_queue.qsize(),
    }


@app.get("/api/results")
async def get_results():
    return {"results": search_results}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    active_connections.append(ws)
    # Send current state on connect
    await ws.send_json({
        "type": "state",
        "current": current_search,
        "queue": queue_items,
        "results": search_results,
    })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in active_connections:
            active_connections.remove(ws)


# Serve frontend
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### 3.3 static/index.html (Frontend completo)

```html
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AwardTool - Busca de Milhas</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a; color: #e2e8f0; min-height: 100vh;
        }
        header {
            background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
            display: flex; align-items: center; justify-content: space-between;
        }
        header h1 { font-size: 20px; color: #38bdf8; }
        header .status { font-size: 13px; display: flex; align-items: center; gap: 8px; }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #ef4444; animation: pulse 2s infinite;
        }
        .status-dot.connected { background: #22c55e; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

        .container { max-width: 900px; margin: 0 auto; padding: 24px; }

        /* Search Form */
        .search-form {
            background: #1e293b; border-radius: 12px; padding: 24px;
            margin-bottom: 24px; border: 1px solid #334155;
        }
        .search-form h2 { font-size: 16px; margin-bottom: 16px; color: #94a3b8; }
        .form-row {
            display: grid; grid-template-columns: 1fr 1fr 1fr 1fr;
            gap: 12px; align-items: end;
        }
        .form-row-2 {
            display: grid; grid-template-columns: 1fr auto;
            gap: 12px; align-items: end; margin-top: 12px;
        }
        .month-selector { display: flex; flex-direction: column; gap: 4px; }
        .month-selector label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }
        .month-mode-tabs {
            display: flex; gap: 0; margin-bottom: 8px;
        }
        .month-mode-tabs button {
            background: #0f172a; border: 1px solid #334155; color: #94a3b8;
            padding: 6px 12px; font-size: 12px; cursor: pointer;
        }
        .month-mode-tabs button:first-child { border-radius: 6px 0 0 6px; }
        .month-mode-tabs button:last-child { border-radius: 0 6px 6px 0; }
        .month-mode-tabs button.active {
            background: #2563eb; border-color: #2563eb; color: white;
        }
        .month-chips {
            display: flex; flex-wrap: wrap; gap: 4px;
        }
        .month-chip {
            background: #0f172a; border: 1px solid #334155; border-radius: 6px;
            padding: 5px 10px; font-size: 12px; color: #94a3b8; cursor: pointer;
            transition: all 0.15s;
        }
        .month-chip:hover { border-color: #38bdf8; color: #e2e8f0; }
        .month-chip.selected {
            background: #1e3a5f; border-color: #38bdf8; color: #38bdf8;
        }
        .form-group { display: flex; flex-direction: column; gap: 4px; }
        .form-group label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }
        .form-group input, .form-group select {
            background: #0f172a; border: 1px solid #334155; border-radius: 8px;
            padding: 10px 12px; color: #e2e8f0; font-size: 14px; outline: none;
        }
        .form-group input:focus, .form-group select:focus { border-color: #38bdf8; }
        .form-group input::placeholder { color: #475569; }
        .btn-search {
            background: #2563eb; color: white; border: none; border-radius: 8px;
            padding: 10px 24px; font-size: 14px; font-weight: 600; cursor: pointer;
            display: flex; align-items: center; gap: 6px; white-space: nowrap;
            height: 42px;
        }
        .btn-search:hover { background: #1d4ed8; }
        .btn-search:disabled { background: #475569; cursor: not-allowed; }

        .form-options {
            display: flex; align-items: center; gap: 16px; margin-top: 12px; flex-wrap: wrap;
        }
        .toggle-label {
            display: flex; align-items: center; gap: 8px; font-size: 13px; color: #94a3b8; cursor: pointer;
        }
        .toggle-label input[type="checkbox"] {
            width: 16px; height: 16px; accent-color: #22c55e;
        }
        .direction-group {
            display: flex; align-items: center; gap: 12px;
        }
        .direction-group span {
            font-size: 13px; color: #64748b; margin-right: 4px;
        }
        .direction-radio {
            display: flex; align-items: center; gap: 5px; font-size: 13px; color: #94a3b8; cursor: pointer;
        }
        .direction-radio input[type="radio"] {
            width: 16px; height: 16px; accent-color: #2563eb;
        }

        /* Queue */
        .section { margin-bottom: 24px; }
        .section-title {
            font-size: 14px; color: #64748b; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 12px;
            display: flex; align-items: center; gap: 8px;
        }
        .badge {
            background: #334155; padding: 2px 8px; border-radius: 10px;
            font-size: 11px; color: #94a3b8;
        }

        .queue-item {
            background: #1e293b; border-radius: 8px; padding: 14px 16px;
            margin-bottom: 8px; border-left: 3px solid #f59e0b;
            display: flex; align-items: center; justify-content: space-between;
        }
        .queue-item.active { border-left-color: #22c55e; background: #1a2e1a; }
        .queue-item .route { font-weight: 600; font-size: 14px; }
        .queue-item .detail { font-size: 12px; color: #94a3b8; }
        .progress-bar {
            width: 200px; height: 6px; background: #334155; border-radius: 3px; overflow: hidden;
        }
        .progress-bar .fill {
            height: 100%; background: #22c55e; border-radius: 3px;
            transition: width 0.5s ease;
        }

        /* Results */
        .result-card {
            background: #1e293b; border-radius: 12px; padding: 20px;
            margin-bottom: 12px; border: 1px solid #334155;
        }
        .result-header {
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 12px;
        }
        .result-header h3 { font-size: 16px; color: #38bdf8; }
        .result-header .time { font-size: 12px; color: #64748b; }
        .result-header .whatsapp-badge {
            background: #166534; color: #4ade80; padding: 2px 8px;
            border-radius: 4px; font-size: 11px;
        }
        .direction { margin-bottom: 12px; }
        .direction-label {
            font-size: 12px; color: #f59e0b; font-weight: 600;
            margin-bottom: 4px;
        }
        .direction-label.return { color: #818cf8; }
        .month-row {
            font-size: 13px; color: #cbd5e1; padding: 2px 0;
        }
        .month-name { color: #94a3b8; }
        .days { color: #e2e8f0; }
        .no-data { color: #475569; font-style: italic; font-size: 13px; }

        /* Empty state */
        .empty-state {
            text-align: center; padding: 48px; color: #475569;
        }
        .empty-state .icon { font-size: 48px; margin-bottom: 12px; }

        /* Responsive */
        @media (max-width: 768px) {
            .form-row { grid-template-columns: 1fr 1fr; }
            .btn-search { grid-column: 1 / -1; justify-content: center; }
        }
    </style>
</head>
<body>
    <header>
        <h1>AwardTool - Busca de Milhas</h1>
        <div class="status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">Desconectado</span>
        </div>
    </header>

    <div class="container">
        <!-- Search Form -->
        <div class="search-form">
            <h2>Nova Busca</h2>
            <div class="form-row">
                <div class="form-group">
                    <label>Origem</label>
                    <input type="text" id="origin" placeholder="GRU" maxlength="3"
                           style="text-transform: uppercase;">
                </div>
                <div class="form-group">
                    <label>Destino</label>
                    <input type="text" id="dest" placeholder="LIS" maxlength="3"
                           style="text-transform: uppercase;">
                </div>
                <div class="form-group">
                    <label>Programa</label>
                    <select id="program">
                        <option value="TP">TAP Miles&Go</option>
                        <option value="G3">Smiles GOL</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Classe</label>
                    <select id="cabin">
                        <option value="economy">Economica</option>
                        <option value="business">Executiva</option>
                        <option value="first">Primeira Classe</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Max Milhas (K)</label>
                    <input type="number" id="maxPrice" placeholder="53" value="53" min="1">
                </div>
            </div>
            <div class="form-row-2">
                <div class="month-selector">
                    <label>Meses</label>
                    <div class="month-mode-tabs">
                        <button class="active" onclick="setMonthMode('all')">Ano todo</button>
                        <button onclick="setMonthMode('select')">Escolher meses</button>
                    </div>
                    <div class="month-chips" id="monthChips" style="display: none;"></div>
                </div>
                <button class="btn-search" id="btnSearch" onclick="addSearch()">
                    Buscar
                </button>
            </div>
            <div class="form-options">
                <div class="direction-group">
                    <span>Direcao:</span>
                    <label class="direction-radio">
                        <input type="radio" name="direction" value="roundtrip" checked>
                        Ida e volta
                    </label>
                    <label class="direction-radio">
                        <input type="radio" name="direction" value="outbound">
                        So ida
                    </label>
                    <label class="direction-radio">
                        <input type="radio" name="direction" value="inbound">
                        So volta
                    </label>
                </div>
                <label class="toggle-label">
                    <input type="checkbox" id="sendWhatsapp" checked>
                    Enviar resultado pro WhatsApp
                </label>
            </div>
        </div>

        <!-- Queue -->
        <div class="section" id="queueSection" style="display: none;">
            <div class="section-title">
                Fila de Buscas <span class="badge" id="queueCount">0</span>
            </div>
            <div id="queueList"></div>
        </div>

        <!-- Results -->
        <div class="section">
            <div class="section-title">
                Resultados <span class="badge" id="resultsCount">0</span>
            </div>
            <div id="resultsList">
                <div class="empty-state">
                    <div class="icon">Busca</div>
                    <p>Nenhuma busca realizada ainda.<br>Preencha os campos acima e clique em Buscar.</p>
                </div>
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let reconnectTimer = null;
        let monthMode = 'all';
        let selectedMonths = [];

        function initMonthChips() {
            const container = document.getElementById('monthChips');
            const names = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
            const now = new Date();
            let startM = now.getMonth() + (now.getDate() > 15 ? 1 : 0);
            let startY = now.getFullYear();
            if (startM > 11) { startM -= 12; startY++; }

            container.innerHTML = '';
            for (let i = 0; i < 12; i++) {
                let m = startM + i;
                let y = startY;
                while (m > 11) { m -= 12; y++; }
                const chip = document.createElement('span');
                chip.className = 'month-chip';
                chip.textContent = `${names[m]}/${String(y).slice(2)}`;
                chip.dataset.month = m + 1;
                chip.dataset.year = y;
                chip.onclick = () => toggleMonth(chip);
                container.appendChild(chip);
            }
        }

        function setMonthMode(mode) {
            monthMode = mode;
            document.querySelectorAll('.month-mode-tabs button').forEach(b => b.classList.remove('active'));
            if (mode === 'all') {
                document.querySelector('.month-mode-tabs button:first-child').classList.add('active');
                document.getElementById('monthChips').style.display = 'none';
                selectedMonths = [];
                document.querySelectorAll('.month-chip').forEach(c => c.classList.remove('selected'));
            } else {
                document.querySelector('.month-mode-tabs button:last-child').classList.add('active');
                document.getElementById('monthChips').style.display = 'flex';
            }
        }

        function toggleMonth(chip) {
            chip.classList.toggle('selected');
            const m = parseInt(chip.dataset.month);
            const y = parseInt(chip.dataset.year);
            if (chip.classList.contains('selected')) {
                if (!selectedMonths.find(s => s.year === y && s.month === m)) {
                    selectedMonths.push({ year: y, month: m });
                }
            } else {
                selectedMonths = selectedMonths.filter(s => !(s.year === y && s.month === m));
            }
        }

        initMonthChips();

        function connectWS() {
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${location.host}/ws`);

            ws.onopen = () => {
                document.getElementById('statusDot').classList.add('connected');
                document.getElementById('statusText').textContent = 'Conectado';
                if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
            };

            ws.onclose = () => {
                document.getElementById('statusDot').classList.remove('connected');
                document.getElementById('statusText').textContent = 'Desconectado';
                if (!reconnectTimer) {
                    reconnectTimer = setInterval(connectWS, 3000);
                }
            };

            ws.onmessage = (e) => {
                const msg = JSON.parse(e.data);
                handleMessage(msg);
            };
        }

        function handleMessage(msg) {
            switch (msg.type) {
                case 'state':
                    if (msg.results) msg.results.forEach(r => renderResult(r));
                    if (msg.queue) msg.queue.forEach(q => addToQueueUI(q));
                    if (msg.current) addToQueueUI(msg.current, true);
                    break;
                case 'queued':
                    addToQueueUI(msg.search);
                    break;
                case 'search_started':
                    markActive(msg.search.id);
                    break;
                case 'progress':
                    updateProgress(msg.search_id, msg.percent, msg.message);
                    break;
                case 'search_completed':
                    removeFromQueue(msg.result.id);
                    renderResult(msg.result, msg.whatsapp_sent);
                    break;
                case 'search_error':
                    removeFromQueue(msg.search_id);
                    break;
            }
        }

        async function addSearch() {
            const origin = document.getElementById('origin').value.trim().toUpperCase();
            const dest = document.getElementById('dest').value.trim().toUpperCase();
            const program = document.getElementById('program').value;
            const maxPrice = parseInt(document.getElementById('maxPrice').value) || 53;
            const cabin = document.getElementById('cabin').value;
            const sendWA = document.getElementById('sendWhatsapp').checked;
            const direction = document.querySelector('input[name="direction"]:checked').value;

            if (!origin || !dest) {
                alert('Preencha Origem e Destino!');
                return;
            }

            if (monthMode === 'select' && selectedMonths.length === 0) {
                alert('Selecione pelo menos um mes!');
                return;
            }

            const payload = {
                origin, dest, program,
                max_price_k: maxPrice,
                cabin: cabin,
                send_whatsapp: sendWA,
                direction: direction,
                months: monthMode === 'all' ? null : selectedMonths,
            };

            const resp = await fetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });

            if (resp.ok) {
                document.getElementById('origin').value = '';
                document.getElementById('dest').value = '';
            }
        }

        function addToQueueUI(item, isActive = false) {
            const section = document.getElementById('queueSection');
            section.style.display = 'block';
            const list = document.getElementById('queueList');

            const div = document.createElement('div');
            div.className = `queue-item${isActive ? ' active' : ''}`;
            div.id = `queue-${item.id}`;
            div.innerHTML = `
                <div>
                    <div class="route">${item.origin} -> ${item.dest}</div>
                    <div class="detail">${item.program} | Max ${item.max_price_k}K</div>
                    <div class="detail" id="progress-msg-${item.id}">${isActive ? 'Buscando...' : 'Na fila'}</div>
                </div>
                <div class="progress-bar">
                    <div class="fill" id="progress-${item.id}" style="width: 0%"></div>
                </div>
            `;
            list.appendChild(div);
            updateQueueCount();
        }

        function markActive(id) {
            const el = document.getElementById(`queue-${id}`);
            if (el) {
                el.classList.add('active');
                const msg = document.getElementById(`progress-msg-${id}`);
                if (msg) msg.textContent = 'Buscando...';
            }
        }

        function updateProgress(id, percent, message) {
            const bar = document.getElementById(`progress-${id}`);
            if (bar) bar.style.width = `${percent}%`;
            const msg = document.getElementById(`progress-msg-${id}`);
            if (msg) msg.textContent = message;
        }

        function removeFromQueue(id) {
            const el = document.getElementById(`queue-${id}`);
            if (el) el.remove();
            updateQueueCount();
        }

        function updateQueueCount() {
            const count = document.getElementById('queueList').children.length;
            document.getElementById('queueCount').textContent = count;
            document.getElementById('queueSection').style.display = count > 0 ? 'block' : 'none';
        }

        function processDirection(data, maxK) {
            if (!data || Object.keys(data).length === 0) {
                return { html: '<div class="no-data">Nenhuma data encontrada</div>', cheapestHtml: '' };
            }

            let allLines = '';
            let cheapestPrice = null;
            let cheapestByMonth = {};

            for (const [month, days] of Object.entries(data)) {
                if (days.length > 0 && typeof days[0] === 'object') {
                    const dayStrs = days.map(d => d.day);
                    allLines += `<div class="month-row"><span class="month-name">${month}:</span> <span class="days">${dayStrs.join(', ')}</span></div>`;

                    for (const d of days) {
                        if (cheapestPrice === null || d.price < cheapestPrice) {
                            cheapestPrice = d.price;
                            cheapestByMonth = {};
                        }
                        if (d.price === cheapestPrice) {
                            if (!cheapestByMonth[month]) cheapestByMonth[month] = [];
                            cheapestByMonth[month].push(d.day);
                        }
                    }
                } else {
                    allLines += `<div class="month-row"><span class="month-name">${month}:</span> <span class="days">${days.join(', ')}</span></div>`;
                }
            }

            let cheapestHtml = '';
            if (cheapestPrice !== null && cheapestPrice < maxK) {
                cheapestHtml = `<div style="margin-top:6px;padding:6px 10px;background:#064e3b;border-radius:6px;font-size:12px;">`;
                cheapestHtml += `<span style="color:#34d399;font-weight:600;">Mais baratas (${cheapestPrice}K):</span> `;
                const parts = [];
                for (const [month, days] of Object.entries(cheapestByMonth)) {
                    parts.push(`${month}: ${days.join(', ')}`);
                }
                cheapestHtml += `<span style="color:#6ee7b7;">${parts.join(' | ')}</span></div>`;
            }

            return { html: allLines, cheapestHtml };
        }

        function renderResult(result, whatsappSent = false) {
            const list = document.getElementById('resultsList');
            const empty = list.querySelector('.empty-state');
            if (empty) empty.remove();

            const card = document.createElement('div');
            card.className = 'result-card';

            const dir = result.direction || 'roundtrip';
            const outbound = processDirection(result.outbound, result.max_price_k);
            const inbound = processDirection(result.inbound, result.max_price_k);

            const waBadge = whatsappSent ? '<span class="whatsapp-badge">WhatsApp</span>' : '';
            const cabinNames = {economy: 'Eco', business: 'Exec', first: '1st'};
            const cabinLabel = cabinNames[result.cabin] || 'Eco';
            const arrow = dir === 'roundtrip' ? '<->' : '->';
            const routeLabel = dir === 'inbound'
                ? `${result.dest} -> ${result.origin}`
                : `${result.origin} ${arrow} ${result.dest}`;
            const dirLabel = dir === 'outbound' ? ' | So ida' : dir === 'inbound' ? ' | So volta' : '';

            let directionsHtml = '';
            if (dir === 'roundtrip' || dir === 'outbound') {
                directionsHtml += `
                <div class="direction">
                    <div class="direction-label">IDA (${result.origin} -> ${result.dest})</div>
                    ${outbound.html}
                    ${outbound.cheapestHtml}
                </div>`;
            }
            if (dir === 'roundtrip' || dir === 'inbound') {
                directionsHtml += `
                <div class="direction">
                    <div class="direction-label return">VOLTA (${result.dest} -> ${result.origin})</div>
                    ${inbound.html}
                    ${inbound.cheapestHtml}
                </div>`;
            }

            card.innerHTML = `
                <div class="result-header">
                    <h3>${routeLabel} (${result.program}, ${cabinLabel}, ${result.max_price_k}K${dirLabel})</h3>
                    <div style="display:flex;gap:8px;align-items:center;">
                        ${waBadge}
                        <span class="time">${result.searched_at || ''}</span>
                    </div>
                </div>
                ${directionsHtml}
            `;

            list.prepend(card);
            document.getElementById('resultsCount').textContent =
                list.querySelectorAll('.result-card').length;
        }

        document.querySelectorAll('.form-group input').forEach(el => {
            el.addEventListener('keydown', e => {
                if (e.key === 'Enter') addSearch();
            });
        });

        connectWS();
    </script>
</body>
</html>
```

### 3.4 config.json (Configuracao)

```json
{
  "evolution_api": {
    "url": "https://evotripse.tripse.com.br",
    "instance": "clarisse",
    "api_key": "SUA_API_KEY_AQUI",
    "destination": "5527998269572"
  },
  "schedule": {
    "monday": {
      "label": "Brasil -> Lisboa (TAP)",
      "program": "TP",
      "max_price_k": 53,
      "routes": [
        {"from": "GRU", "to": "LIS"},
        {"from": "REC", "to": "LIS"},
        {"from": "GIG", "to": "LIS"},
        {"from": "CNF", "to": "LIS"},
        {"from": "BSB", "to": "LIS"},
        {"from": "SSA", "to": "LIS"},
        {"from": "NAT", "to": "LIS"},
        {"from": "FOR", "to": "LIS"},
        {"from": "BEL", "to": "LIS"},
        {"from": "POA", "to": "LIS"}
      ]
    },
    "tuesday": {
      "label": "Brasil -> Buenos Aires (Smiles GOL)",
      "program": "G3",
      "max_price_k": 35,
      "routes": [
        {"from": "VIX", "to": "AEP"},
        {"from": "CNF", "to": "AEP"},
        {"from": "SSA", "to": "AEP"},
        {"from": "BSB", "to": "AEP"},
        {"from": "GIG", "to": "AEP"},
        {"from": "FOR", "to": "AEP"},
        {"from": "PMW", "to": "AEP"},
        {"from": "MAO", "to": "AEP"},
        {"from": "BEL", "to": "AEP"},
        {"from": "GYN", "to": "AEP"}
      ]
    },
    "wednesday": {
      "label": "Brasil -> Punta Cana (Smiles GOL)",
      "program": "G3",
      "max_price_k": 70,
      "routes": [
        {"from": "VIX", "to": "PUJ"},
        {"from": "CNF", "to": "PUJ"},
        {"from": "SSA", "to": "PUJ"},
        {"from": "FLN", "to": "PUJ"},
        {"from": "MAO", "to": "PUJ"},
        {"from": "CWB", "to": "PUJ"},
        {"from": "IGU", "to": "PUJ"},
        {"from": "GIG", "to": "PUJ"},
        {"from": "CGR", "to": "PUJ"},
        {"from": "AJU", "to": "PUJ"},
        {"from": "THE", "to": "PUJ"}
      ]
    }
  }
}
```

### 3.5 requirements.txt

```
playwright>=1.40.0
fastapi>=0.100.0
uvicorn>=0.23.0
websockets>=11.0
aiohttp>=3.9.0
```

### 3.6 Dockerfile

```dockerfile
FROM python:3.11-slim

# Install Chrome dependencies
RUN apt-get update && apt-get install -y \
    wget gnupg2 curl \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

# Copy app
COPY search_engine.py server.py ./
COPY static/ ./static/

EXPOSE 8000

CMD ["python", "server.py"]
```

### 3.7 docker-compose.yml

```yaml
version: '3.8'

services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./profiles:/app/profiles
      - ./data:/app/data
    env_file:
      - .env
    restart: unless-stopped
    shm_size: '2gb'  # Chrome precisa de shared memory

  # Descomentar quando implementar banco de dados
  # db:
  #   image: postgres:15-alpine
  #   environment:
  #     POSTGRES_DB: awardtool
  #     POSTGRES_USER: app
  #     POSTGRES_PASSWORD: ${DB_PASSWORD:-changeme}
  #   volumes:
  #     - pgdata:/var/lib/postgresql/data
  #   restart: unless-stopped

# volumes:
#   pgdata:
```

### 3.8 .env.example

```
# Evolution API (WhatsApp)
EVOLUTION_API_URL=https://evotripse.tripse.com.br
EVOLUTION_API_KEY=SUA_API_KEY_AQUI
EVOLUTION_INSTANCE=clarisse
WHATSAPP_NUMBER=5527998269572

# Server
PORT=8000
HOST=0.0.0.0

# Playwright
HEADLESS=true
DELAY_BETWEEN_SEARCHES=15

# Database (para VPS)
# DATABASE_URL=postgresql://app:senha@localhost:5432/awardtool
```

### 3.9 accounts.example.json

```json
[
  {
    "id": "conta1",
    "email": "usuario1@email.com",
    "password": "senha_aqui",
    "profile_dir": "./profiles/conta1",
    "status": "active",
    "notes": "Conta principal"
  },
  {
    "id": "conta2",
    "email": "usuario2@email.com",
    "password": "senha_aqui",
    "profile_dir": "./profiles/conta2",
    "status": "active",
    "notes": ""
  }
]
```

### 3.10 .gitignore

```
venv/
.browser-profile/
profiles/
__pycache__/
*.pyc
.env
accounts.json
config.json
resultados/
data/
*.db
```

### 3.11 setup.sh

```bash
#!/bin/bash
echo "Configurando AwardTool Scraper..."

# Cria virtual environment
python3 -m venv venv
source venv/bin/activate

# Instala dependencias
pip install -r requirements.txt

# Instala navegador Chromium para Playwright
playwright install chromium

echo ""
echo "Setup concluido!"
echo ""
echo "Para rodar:"
echo "  source venv/bin/activate"
echo "  python server.py"
```

---

## 4. COMO O SCRAPING FUNCIONA

### 4.1 URL do AwardTool
Cada busca gera uma URL no padrao:
```
https://www.awardtool.com/flight?flightWay=oneway&pax=1&children=0
  &cabins=Economy%26Premium+Economy%26Business%26First
  &range=true&rangeV2=false
  &from={IATA_ORIGEM}&to={IATA_DESTINO}
  &programs={CODIGO_PROGRAMA}
  &oneWayRangeStartDate={UNIX_TIMESTAMP_INICIO_MES}
  &oneWayRangeEndDate={UNIX_TIMESTAMP_FIM_MES}
```

### 4.2 Extracao via JavaScript
O AwardTool renderiza os dados no DOM como texto. A regex captura linhas no formato:
```
Mon 05/01        <-- Data (match[1])
Only
53K              <-- Economy (match[2])
85K              <-- Premium Economy (match[3])
120K             <-- Business (match[4])
250K             <-- First (match[5])
```

O indice usado depende da classe selecionada:
- economy = match[2]
- business = match[4]
- first = match[5]

### 4.3 Delay entre buscas
O AwardTool bloqueia se fizer muitas requisicoes rapidas. O delay atual e **20 segundos** entre cada pagina. Isso significa:
- 1 rota ida+volta, 12 meses = 24 paginas = ~8 minutos
- 1 rota so ida, 12 meses = 12 paginas = ~4 minutos
- 1 rota, 1 mes = 1-2 paginas = ~20-40 segundos

### 4.4 Perfil persistente do Chrome
O diretorio `.browser-profile/` armazena cookies e sessao do AwardTool. Quando o usuario faz login uma vez, o login persiste entre execucoes.

**IMPORTANTE**: Nao e possivel abrir duas instancias do Playwright com o mesmo perfil simultaneamente. Se o servidor estiver rodando, feche-o antes de executar o script de login.

### 4.5 Chrome Real vs Chromium
O AwardTool detecta e bloqueia o Chromium de teste do Playwright. Por isso usamos `channel="chrome"` para abrir o Chrome real instalado no sistema.

---

## 5. API REST

### POST /api/search
Adiciona uma busca na fila.

```json
{
  "origin": "GRU",
  "dest": "LIS",
  "program": "TP",
  "max_price_k": 53,
  "cabin": "economy",
  "direction": "roundtrip",
  "send_whatsapp": true,
  "months": null
}
```

**Campos:**
| Campo | Tipo | Padrao | Descricao |
|-------|------|--------|-----------|
| origin | str | - | Codigo IATA origem (3 letras) |
| dest | str | - | Codigo IATA destino (3 letras) |
| program | str | - | "TP" (TAP) ou "G3" (Smiles GOL) |
| max_price_k | int | - | Limite maximo de milhas (ex: 53 = 53.000) |
| cabin | str | "economy" | "economy", "business" ou "first" |
| direction | str | "roundtrip" | "roundtrip", "outbound" ou "inbound" |
| send_whatsapp | bool | true | Enviar resultado via WhatsApp |
| months | list/null | null | null = 12 meses, ou lista de {year, month} |

### GET /api/queue
Retorna estado atual da fila.

### GET /api/results
Retorna todos os resultados ja buscados.

### WebSocket /ws
Conexao em tempo real para receber progresso das buscas.

**Tipos de mensagem:**
- `state` - Estado inicial ao conectar
- `queued` - Nova busca adicionada na fila
- `search_started` - Busca iniciou
- `progress` - Progresso da busca (step, total, percent, message)
- `search_completed` - Busca finalizada com resultado
- `search_error` - Erro na busca

---

## 6. WHATSAPP (Evolution API)

O sistema envia resultados via Evolution API. Configuracao no `config.json`:

```json
{
  "evolution_api": {
    "url": "https://evotripse.tripse.com.br",
    "instance": "clarisse",
    "api_key": "SUA_API_KEY",
    "destination": "5527998269572"
  }
}
```

**Formato da mensagem WhatsApp:**
```
Oportunidade de resgate - Internacional
Programa de fidelidade: TAP Miles&Go
Classe: Economica

Origem: Sao Paulo (GRU)
Destino: Lisboa (LIS)

Quantidade de milhas: a partir de 53 mil milhas o trecho

Datas de ida:
Maio 2026: 10, 15, 22
Junho 2026: 03, 18

Datas de volta:
Maio 2026: 12, 20
Junho 2026: 05, 25
```

---

## 7. COMO RODAR LOCALMENTE

### Setup inicial
```bash
cd awardtool-scraper
chmod +x setup.sh
./setup.sh
```

### Login no AwardTool (primeira vez)
O AwardTool requer login. Como o Playwright usa um perfil separado do Chrome do usuario, e necessario fazer login manualmente:

```bash
source venv/bin/activate
python3 -c "
import asyncio
from playwright.async_api import async_playwright

async def login():
    pw = await async_playwright().__aenter__()
    ctx = await pw.chromium.launch_persistent_context(
        '.browser-profile', channel='chrome', headless=False,
        viewport={'width': 1920, 'height': 1080},
        args=['--disable-blink-features=AutomationControlled']
    )
    page = ctx.pages[0]
    await page.goto('https://www.awardtool.com')
    print('Faca login no AwardTool. Voce tem 90 segundos...')
    await asyncio.sleep(90)
    await ctx.close()
    print('Login salvo!')

asyncio.run(login())
"
```

### Iniciar o servidor
```bash
source venv/bin/activate
python3 server.py
```
Acesse: http://localhost:8000

---

## 8. ESCALANDO PARA VPS (20 CONTAS)

### Estrategia
Cada conta AwardTool tem um limite de buscas. Para escalar:

1. **1 instancia Docker por conta** - Cada container tem seu proprio perfil Chrome
2. **Load balancer (Nginx)** distribui buscas entre contas
3. **Fila centralizada (Redis)** gerencia qual conta faz qual busca
4. **Rotacao de contas** - Se uma conta for bloqueada, usa a proxima

### Estrutura sugerida para VPS
```
/opt/awardtool/
|-- docker-compose.yml    # Orquestra todos os containers
|-- nginx.conf            # Reverse proxy + load balancer
|-- accounts.json         # 20 contas com credenciais
|-- profiles/
|   |-- conta1/           # Perfil Chrome conta 1
|   |-- conta2/           # Perfil Chrome conta 2
|   |-- ...
|   |-- conta20/          # Perfil Chrome conta 20
|-- shared/
|   |-- config.json       # Config compartilhada
```

### Estimativa de custos VPS
Para 20 contas rodando simultaneamente:
- **RAM**: ~500MB por instancia Chrome = 10GB minimo
- **CPU**: 4-8 vCPUs
- **Disco**: 20GB SSD
- **VPS recomendado**: Hetzner CPX31 (~EUR 15/mes) ou similar
- **Total estimado**: EUR 15-25/mes

### Pontos criticos para o desenvolvedor
1. **headless=False e necessario** - O AwardTool detecta headless. Em VPS, usar Xvfb (virtual display)
2. **Login manual por conta** - Cada conta precisa de login manual inicial. Automatizar se possivel
3. **Rate limiting** - Maximo ~3 rotas por hora por conta para evitar bloqueio
4. **Monitoramento** - Implementar health checks e alertas quando uma conta for bloqueada
5. **Rotacao** - Se uma conta travar, redistribuir buscas para as outras

---

## 9. PROBLEMAS CONHECIDOS E SOLUCOES

| Problema | Causa | Solucao |
|----------|-------|---------|
| AwardTool nao carrega dados | Bot detection | Usar `channel="chrome"` (Chrome real) |
| "Browser profile locked" | Duas instancias com mesmo perfil | Fechar servidor antes de fazer login |
| Bloqueio temporario | Muitas buscas rapidas | Aumentar DELAY_BETWEEN_SEARCHES (atualmente 20s) |
| `ModuleNotFoundError: fastapi` | Python errado | Usar `./venv/bin/python` ou `source venv/bin/activate` |
| `asyncio.Queue` erro de loop | Queue criada fora do event loop | Criar queue dentro de `@app.on_event("startup")` |
| `type | None` syntax error | Python < 3.10 | Usar `Optional[Type]` do typing |
| Dados nao aparecem na pagina | AwardTool carregamento lento | Aumentar delay para 15-20s |

---

## 10. ROTAS AGENDADAS (SCHEDULE)

O config.json define rotas que podem ser executadas automaticamente:

| Dia | Destino | Programa | Max Milhas | Rotas |
|-----|---------|----------|------------|-------|
| Segunda | Lisboa (LIS) | TAP Miles&Go | 53K | 10 origens brasileiras |
| Terca | Buenos Aires (AEP) | Smiles GOL | 35K | 10 origens brasileiras |
| Quarta | Punta Cana (PUJ) | Smiles GOL | 70K | 11 origens brasileiras |

**Para implementar agendamento automatico no VPS**, usar cron ou APScheduler para disparar as buscas nos horarios definidos.

---

## 11. PROXIMOS PASSOS SUGERIDOS

1. [ ] Persistencia de resultados em banco de dados (PostgreSQL)
2. [ ] Painel administrativo para gerenciar contas
3. [ ] Sistema de agendamento integrado (APScheduler ou Celery)
4. [ ] Monitoramento com health checks e alertas
5. [ ] Login automatico no AwardTool (se possivel)
6. [ ] Cache de resultados para evitar buscas duplicadas
7. [ ] Dashboard com historico de precos e graficos
8. [ ] Notificacoes por email alem do WhatsApp
