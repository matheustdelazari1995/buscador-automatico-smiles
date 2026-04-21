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
SEARCHES_BEFORE_PAUSE = 8    # after N months searched, take a long pause
LONG_PAUSE_SECONDS = 60      # duration of preventive pause
BLOCK_DETECTION_KEYWORDS = [
    # AwardTool's specific popup
    "searching too frequently",
    "excessive number of searches",
    "performed an excessive number",
    "please wait for a few minutes",
    "wait for a few minutes and try again",
    # Generic
    "too many requests",
    "rate limit",
    "try again later",
    "temporarily blocked",
    "access denied",
    "muitas buscas",
    "limite de buscas",
]
# After this many CONSECUTIVE months with 0 results, suspect block
SUSPECT_BLOCK_AFTER_EMPTY = 5


class AwardToolBlocked(Exception):
    """Raised when AwardTool is detected as blocked.
    Carries partial results so the worker can save progress and resume later.
    """
    def __init__(self, message, partial_direction=None, outbound=None, inbound=None):
        super().__init__(message)
        self.partial_direction = partial_direction or {}  # months found so far in the direction that was blocked
        self.outbound = outbound  # full outbound data (filled by search_route)
        self.inbound = inbound    # full inbound data (filled by search_route)


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
        if (m) {{
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
    def __init__(self, profile_dir=None, account_id=None):
        self.playwright = None
        self.context = None
        self.page = None
        self._started = False
        self._searches_since_pause = 0  # global counter across all routes
        self.pause_cb = None  # optional: async fn(seconds) called before long pause
        self.profile_dir = profile_dir or ".browser-profile"
        self.account_id = account_id or "default"

    async def start(self):
        if self._started:
            return
        # Allow absolute or relative path; relative = next to this file
        if os.path.isabs(self.profile_dir):
            browser_profile = self.profile_dir
        else:
            browser_profile = os.path.join(os.path.dirname(__file__), self.profile_dir)
        os.makedirs(browser_profile, exist_ok=True)
        self.playwright = await async_playwright().__aenter__()
        # Use the real Chrome installed on the system (not Chromium test binary)
        # This avoids bot detection since it's the actual Chrome browser
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
        print(f"[Engine {self.account_id}] Chrome iniciado com perfil {browser_profile}")

    async def stop(self):
        if self.context:
            await self.context.close()
        self._started = False

    async def _check_block(self):
        """Check if the current page indicates AwardTool has blocked us."""
        try:
            page_text = await self.page.evaluate("() => document.body.innerText.toLowerCase()")
            for kw in BLOCK_DETECTION_KEYWORDS:
                if kw in page_text:
                    return True, kw
            # Also check if page has almost no content (likely blocked/empty response)
            if len(page_text.strip()) < 200:
                return True, "empty_page"
            return False, None
        except Exception:
            return False, None

    async def _preventive_pause_if_needed(self):
        """Every SEARCHES_BEFORE_PAUSE months, pause LONG_PAUSE_SECONDS to avoid bot detection."""
        self._searches_since_pause += 1
        if self._searches_since_pause >= SEARCHES_BEFORE_PAUSE:
            self._searches_since_pause = 0
            if self.pause_cb:
                try:
                    await self.pause_cb(LONG_PAUSE_SECONDS)
                except Exception:
                    pass
            await asyncio.sleep(LONG_PAUSE_SECONDS)
            if self.pause_cb:
                try:
                    await self.pause_cb(0)  # 0 = pause ended
                except Exception:
                    pass

    async def _search_direction(self, origin, dest, program, max_price_k, months, progress_cb=None, cabin="economy"):
        """Search one direction (e.g. VIX->AEP) for all months.
        Raises AwardToolBlocked when the site is detected as blocked."""
        cabin_index = CABIN_MATCH_INDEX.get(cabin, 2)
        results = {}
        consecutive_empty = 0

        for i, month in enumerate(months):
            url = build_url(origin, dest, program, month["start"], month["end"])
            try:
                # Timeout de 45s: se a pagina nao carregar em 45s, desiste desse mes
                # (evita que o Chrome trave indefinidamente aguardando carregamento)
                await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(DELAY_BETWEEN_SEARCHES)

                # Check for explicit block keywords
                blocked, reason = await self._check_block()
                if blocked:
                    raise AwardToolBlocked(
                        f"Bloqueio detectado: {reason}",
                        partial_direction=results,
                    )

                js = JS_EXTRACT_TEMPLATE.format(cabin_index=cabin_index)
                data = await self.page.evaluate(js)

                # Always record that we searched this month (even if 0 results)
                # This lets us resume from correct position after a block.
                results[month["name"]] = data["days"]
                if data["count"] > 0:
                    consecutive_empty = 0
                else:
                    # Also check: was the page actually loaded with data,
                    # or is this "empty" really a block? Look at raw matches
                    raw_js = """() => {
                        const text = document.body.innerText;
                        const regex = /((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\\s+\\d{2}\\/\\d{2})/g;
                        let c = 0; while (regex.exec(text) !== null) c++;
                        return c;
                    }"""
                    raw_count = await self.page.evaluate(raw_js)
                    if raw_count == 0:
                        consecutive_empty += 1
                    else:
                        consecutive_empty = 0

                if consecutive_empty >= SUSPECT_BLOCK_AFTER_EMPTY:
                    raise AwardToolBlocked(
                        f"{consecutive_empty} meses seguidos sem dados (suspeita de bloqueio)",
                        partial_direction=results,
                    )

                if progress_cb:
                    await progress_cb(i + 1, len(months), month["name"], data["count"])

                # Preventive pause: every SEARCHES_BEFORE_PAUSE months, pause LONG_PAUSE_SECONDS
                # Skip pause after the VERY LAST month (no need to pause if we're done)
                if i < len(months) - 1:
                    await self._preventive_pause_if_needed()

            except AwardToolBlocked:
                raise
            except Exception as e:
                if progress_cb:
                    await progress_cb(i + 1, len(months), month["name"], -1, str(e))

        return results

    async def search_route(self, origin, dest, program, max_price_k, progress_cb=None, selected_months=None, direction="roundtrip", cabin="economy", existing_result=None):
        """
        Full search: outbound + return for a route.
        selected_months: None = all 12 months, or list of {"year": int, "month": int}
        direction: "roundtrip" (ida+volta), "outbound" (só ida), "inbound" (só volta)
        cabin: "economy" or "business" or "first"
        progress_cb: async fn(step, total_steps, detail_msg)
        existing_result: partial result from a previous blocked attempt. Months present
            in existing_result.outbound / existing_result.inbound will be skipped.
        """
        if not self._started:
            await self.start()

        all_months = get_months_to_search()

        if selected_months:
            # Filter to only selected months
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

        # Resume: skip months already present in existing_result
        existing_outbound = (existing_result or {}).get("outbound") or {}
        existing_inbound = (existing_result or {}).get("inbound") or {}
        outbound = dict(existing_outbound)
        inbound = dict(existing_inbound)

        outbound_months_to_do = [m for m in months if m["name"] not in existing_outbound] if do_outbound else []
        inbound_months_to_do = [m for m in months if m["name"] not in existing_inbound] if do_inbound else []
        total_steps = len(outbound_months_to_do) + len(inbound_months_to_do)
        offset = len(outbound_months_to_do) if do_outbound else 0

        if do_outbound and outbound_months_to_do:
            async def ida_progress(i, total, month_name, count, error=None):
                if progress_cb:
                    msg = f"IDA {origin}->{dest}: {month_name}"
                    if error:
                        msg += f" (erro: {error})"
                    elif count >= 0:
                        msg += f" ({count} datas)"
                    await progress_cb(i, total_steps, msg)

            try:
                new_outbound = await self._search_direction(
                    origin, dest, program, max_price_k, outbound_months_to_do,
                    ida_progress, cabin=cabin,
                )
                outbound.update(new_outbound)
            except AwardToolBlocked as e:
                outbound.update(e.partial_direction or {})
                e.outbound = outbound
                e.inbound = inbound
                raise

        if do_inbound and inbound_months_to_do:
            async def volta_progress(i, total, month_name, count, error=None):
                if progress_cb:
                    msg = f"VOLTA {dest}->{origin}: {month_name}"
                    if error:
                        msg += f" (erro: {error})"
                    elif count >= 0:
                        msg += f" ({count} datas)"
                    await progress_cb(offset + i, total_steps, msg)

            try:
                new_inbound = await self._search_direction(
                    dest, origin, program, max_price_k, inbound_months_to_do,
                    volta_progress, cabin=cabin,
                )
                inbound.update(new_inbound)
            except AwardToolBlocked as e:
                inbound.update(e.partial_direction or {})
                e.outbound = outbound
                e.inbound = inbound
                raise

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


def _extract_days_and_cheapest(direction_data, max_price=None):
    """
    From direction data (dict of month -> list of {day, price}),
    returns (formatted_lines, cheapest_price, cheapest_lines).
    If max_price is given, only includes days with price <= max_price.
    """
    all_lines = []
    cheapest_price = None
    cheapest_by_month = {}

    for month_name, days in direction_data.items():
        if not days:
            continue
        if isinstance(days[0], dict):
            filtered = [d for d in days if max_price is None or d["price"] <= max_price]
            if not filtered:
                continue
            day_strs = [d["day"] for d in filtered]
            all_lines.append(f"{month_name}: {', '.join(day_strs)}")

            for d in filtered:
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


def get_min_price(result):
    """Find the minimum price (in K) across outbound + inbound in a result."""
    min_p = None
    for direction_data in (result.get("outbound", {}), result.get("inbound", {})):
        for days in direction_data.values():
            for d in days:
                if isinstance(d, dict):
                    p = d.get("price")
                    if p is not None and (min_p is None or p < min_p):
                        min_p = p
    return min_p


def format_result_text(result, max_price_filter=None):
    """Format search result for WhatsApp.
    max_price_filter: if given (int K), filter days to only those with price <= this value.
    """
    origin = result["origin"]
    dest = result["dest"]
    program = result["program"]
    # Use filter if provided, else fall back to result's own max_price_k
    max_k = max_price_filter if max_price_filter is not None else result.get("max_price_k")

    origin_city = IATA_TO_CITY.get(origin, origin)
    dest_city = IATA_TO_CITY.get(dest, dest)
    program_name = PROGRAM_NAMES.get(program, program)

    is_national = origin in BRAZIL_IATA and dest in BRAZIL_IATA
    scope = "Nacional" if is_national else "Internacional"

    # Extract data with cheapest info, filtering by max_k
    ida_lines, ida_cheapest_price, ida_cheapest = _extract_days_and_cheapest(result.get("outbound", {}), max_k)
    volta_lines, volta_cheapest_price, volta_cheapest = _extract_days_and_cheapest(result.get("inbound", {}), max_k)

    # Find the real minimum price
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
