"""
Mapeamentos pre-construidos: regioes -> IATAs, feriados BR, meses.
Usado pelo agente conversacional pra entender perguntas naturais
("Europa em julho", "Carnaval 2027", "Sudeste pra Caribe", etc).
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime
from calendar import monthrange


# ============== REGIOES ==============
# Origens brasileiras agrupadas por regiao (IATAs comuns que voos
# domesticos saem). Usuario fala "saindo do Sudeste" e agente expande.
REGIONS: Dict[str, List[str]] = {
    # Brasil
    "sudeste": ["GRU", "CGH", "VCP", "GIG", "SDU", "CNF", "VIX"],
    "sul": ["POA", "CWB", "FLN", "NVT", "JOI"],
    "nordeste": [
        "FOR", "REC", "SSA", "NAT", "MCZ", "JPA", "AJU",
        "SLZ", "THE", "BPS", "FEN",
    ],
    "norte": ["MAO", "BEL", "PVH", "BVB", "MCP", "RBR", "STM"],
    "centro_oeste": ["BSB", "CGB", "CGR", "GYN", "PMW"],

    # Internacional - corredores comuns dos brasileiros
    "europa": [
        "LIS", "OPO", "MAD", "BCN", "CDG", "ORY", "FCO", "MXP",
        "LHR", "FRA", "MUC", "AMS", "ZRH", "ATH", "DUB", "BRU",
    ],
    "eua": [
        "MIA", "JFK", "LAX", "MCO", "ORD", "BOS", "EWR", "FLL",
        "ATL", "DFW", "SFO", "LAS", "IAH",
    ],
    "caribe": [
        "CUN", "PUJ", "AUA", "CCS", "VRA", "POP", "MBJ",
        "NAS", "STT", "HAV",
    ],
    "america_sul": [
        "AEP", "EZE", "MVD", "SCL", "BOG", "LIM", "CUZ", "PTY",
        "VVI", "ASU", "MDZ", "COR", "ROS", "CTG", "ADZ", "CCS",
    ],
    "america_norte": ["MIA", "JFK", "LAX", "MCO", "ORD", "BOS", "YYZ", "YVR", "MEX"],
    "africa": ["JNB", "CPT", "ADD", "CAI"],
    "asia": ["HND", "NRT", "ICN", "BKK", "SIN", "DXB", "DOH"],
    "oceania": ["SYD", "MEL", "AKL"],
}

# Aliases (varia\u00e7\u00f5es comuns escritas pelo user)
REGION_ALIASES = {
    "ne": "nordeste", "norte_nordeste": "nordeste", "sudeste": "sudeste",
    "se": "sudeste", "centro-oeste": "centro_oeste", "co": "centro_oeste",
    "estados unidos": "eua", "estados_unidos": "eua", "usa": "eua", "us": "eua",
    "america central": "caribe", "america_central": "caribe",
    "america latina": "america_sul", "america_latina": "america_sul",
    "europa": "europa", "ue": "europa", "european union": "europa",
}


def region_to_iatas(name: str) -> List[str]:
    """Traduz nome de regiao em lista de IATAs.
    Aceita variantes (case-insensitive, com aliases).
    Retorna [] se nao encontrar."""
    if not name:
        return []
    key = name.lower().strip().replace(" ", "_")
    key = REGION_ALIASES.get(key, key)
    return list(REGIONS.get(key, []))


def list_known_regions() -> List[str]:
    return sorted(REGIONS.keys())


# ============== NOMES IATA -> CIDADE ==============
# Nome legivel pra agente apresentar respostas mais naturais
IATA_NAMES: Dict[str, str] = {
    # Brasil
    "GRU": "Sao Paulo (GRU)", "CGH": "Sao Paulo (Congonhas)", "VCP": "Campinas",
    "GIG": "Rio de Janeiro (GIG)", "SDU": "Rio de Janeiro (Santos Dumont)",
    "BSB": "Brasilia", "CNF": "Belo Horizonte", "VIX": "Vitoria",
    "CWB": "Curitiba", "POA": "Porto Alegre", "FLN": "Florianopolis",
    "FOR": "Fortaleza", "REC": "Recife", "SSA": "Salvador", "NAT": "Natal",
    "MCZ": "Maceio", "JPA": "Joao Pessoa", "AJU": "Aracaju", "BPS": "Porto Seguro",
    "MAO": "Manaus", "BEL": "Belem", "GYN": "Goiania", "CGB": "Cuiaba",
    "CGR": "Campo Grande", "PMW": "Palmas", "SLZ": "Sao Luis", "THE": "Teresina",

    # Internacional
    "JFK": "Nova York (JFK)", "EWR": "Nova York (Newark)", "MIA": "Miami",
    "MCO": "Orlando", "LAX": "Los Angeles", "ORD": "Chicago", "BOS": "Boston",
    "FLL": "Fort Lauderdale", "ATL": "Atlanta", "DFW": "Dallas",
    "LIS": "Lisboa", "OPO": "Porto", "MAD": "Madri", "BCN": "Barcelona",
    "CDG": "Paris (CDG)", "ORY": "Paris (Orly)", "FCO": "Roma", "MXP": "Milao",
    "LHR": "Londres", "FRA": "Frankfurt", "MUC": "Munique", "AMS": "Amsterda",
    "ZRH": "Zurique",
    "AEP": "Buenos Aires (Aeroparque)", "EZE": "Buenos Aires (Ezeiza)",
    "MVD": "Montevideu", "SCL": "Santiago", "BOG": "Bogota", "LIM": "Lima",
    "CUZ": "Cusco", "PTY": "Cidade do Panama", "VVI": "Santa Cruz (Bolivia)",
    "ASU": "Assuncao", "MDZ": "Mendoza", "COR": "Cordoba", "ROS": "Rosario",
    "CTG": "Cartagena", "ADZ": "San Andres",
    "CUN": "Cancun", "PUJ": "Punta Cana", "AUA": "Aruba", "CCS": "Caracas",
    "JNB": "Joanesburgo", "CPT": "Cidade do Cabo",
    "DXB": "Dubai", "DOH": "Doha", "HND": "Toquio (Haneda)", "NRT": "Toquio (Narita)",
    "ICN": "Seul", "BKK": "Bangkok", "SIN": "Singapura",
    "SYD": "Sydney", "MEL": "Melbourne",
}


def iata_name(iata: str) -> str:
    """Nome legivel da cidade. Cai no IATA cru se nao mapeado."""
    return IATA_NAMES.get((iata or "").upper(), iata)


# ============== MESES PT-BR ==============
MONTHS_PT: Dict[str, int] = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
    # abreviacoes
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def month_date_range(month_name: str, year: int) -> Optional[Tuple[str, str]]:
    """('julho', 2026) -> ('2026-07-01', '2026-07-31')."""
    if not month_name or not year:
        return None
    m = MONTHS_PT.get(month_name.lower().strip())
    if not m:
        return None
    _, last_day = monthrange(int(year), m)
    return (f"{int(year):04d}-{m:02d}-01", f"{int(year):04d}-{m:02d}-{last_day:02d}")


# ============== FERIADOS BR ==============
# Datas oficiais 2026/2027/2028 - feriados que mais geram busca de
# passagem (calculados manualmente pra evitar dependencia de lib externa)
HOLIDAYS: Dict[str, Dict[int, Tuple[str, str]]] = {
    "carnaval": {
        2026: ("2026-02-14", "2026-02-18"),
        2027: ("2027-02-06", "2027-02-10"),
        2028: ("2028-02-26", "2028-03-01"),
    },
    "semana_santa": {  # Sex-Santa, Sab, Pascoa, Seg
        2026: ("2026-04-03", "2026-04-06"),
        2027: ("2027-03-26", "2027-03-29"),
        2028: ("2028-04-14", "2028-04-17"),
    },
    "tiradentes": {  # 21/04 (1 dia)
        2026: ("2026-04-21", "2026-04-21"),
        2027: ("2027-04-21", "2027-04-21"),
        2028: ("2028-04-21", "2028-04-21"),
    },
    "dia_do_trabalho": {  # 1/05
        2026: ("2026-05-01", "2026-05-01"),
        2027: ("2027-05-01", "2027-05-01"),
        2028: ("2028-05-01", "2028-05-01"),
    },
    "corpus_christi": {  # quinta apos pascoa
        2026: ("2026-06-04", "2026-06-04"),
        2027: ("2027-05-27", "2027-05-27"),
        2028: ("2028-06-15", "2028-06-15"),
    },
    "independencia": {  # 7/9
        2026: ("2026-09-07", "2026-09-07"),
        2027: ("2027-09-07", "2027-09-07"),
        2028: ("2028-09-07", "2028-09-07"),
    },
    "nossa_senhora": {  # 12/10 - Padroeira do Brasil
        2026: ("2026-10-12", "2026-10-12"),
        2027: ("2027-10-12", "2027-10-12"),
        2028: ("2028-10-12", "2028-10-12"),
    },
    "finados": {  # 2/11
        2026: ("2026-11-02", "2026-11-02"),
        2027: ("2027-11-02", "2027-11-02"),
        2028: ("2028-11-02", "2028-11-02"),
    },
    "proclamacao_republica": {  # 15/11
        2026: ("2026-11-15", "2026-11-15"),
        2027: ("2027-11-15", "2027-11-15"),
        2028: ("2028-11-15", "2028-11-15"),
    },
    "natal": {  # 24-26/12 (cobre vespera+dia)
        2026: ("2026-12-24", "2026-12-26"),
        2027: ("2027-12-24", "2027-12-26"),
        2028: ("2028-12-24", "2028-12-26"),
    },
    "reveillon": {  # 30/12 a 02/01 do ano seguinte
        2026: ("2026-12-30", "2027-01-02"),
        2027: ("2027-12-30", "2028-01-02"),
        2028: ("2028-12-30", "2029-01-02"),
    },
}

HOLIDAY_LABELS = {
    "carnaval": "Carnaval",
    "semana_santa": "Semana Santa / Pascoa",
    "tiradentes": "Tiradentes (21/04)",
    "dia_do_trabalho": "Dia do Trabalho (01/05)",
    "corpus_christi": "Corpus Christi",
    "independencia": "Independencia (07/09)",
    "nossa_senhora": "Nossa Senhora (12/10)",
    "finados": "Finados (02/11)",
    "proclamacao_republica": "Proclamacao da Republica (15/11)",
    "natal": "Natal",
    "reveillon": "Reveillon",
}


def holiday_date_range(holiday: str, year: int) -> Optional[Tuple[str, str]]:
    """('carnaval', 2027) -> ('2027-02-06', '2027-02-10'). None se nao tiver."""
    return HOLIDAYS.get(holiday.lower().strip().replace(" ", "_"), {}).get(int(year))


def list_holidays(year: Optional[int] = None, from_date: Optional[str] = None) -> List[Dict]:
    """Lista feriados, opcionalmente filtrando por ano e/ou data limite."""
    out = []
    for h_key, by_year in HOLIDAYS.items():
        for y, (start, end) in by_year.items():
            if year is not None and y != year:
                continue
            if from_date and end < from_date:
                continue
            out.append({
                "key": h_key,
                "label": HOLIDAY_LABELS.get(h_key, h_key),
                "year": y,
                "start": start,
                "end": end,
            })
    out.sort(key=lambda x: x["start"])
    return out
