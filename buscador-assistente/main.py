"""
Assistente IA do Buscador Smiles. FastAPI + Claude Haiku 4.5 com tool-calling.
Le routes.json + results.json read-only do scraper, responde sem inventar dados.
"""

import json
import os
from typing import List, Optional, Dict, Any
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from anthropic import Anthropic

import data_loader
import regions


# ============== CONFIG ==============
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048
MAX_TOOL_ITERATIONS = 8
TOOL_RESULT_MAX_CHARS = 8000  # truncar pra evitar context explosion

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_client: Optional[Anthropic] = None


def _get_client() -> Optional[Anthropic]:
    """Lazy: so cria client se a key existir.
    Permite o service rodar sem key (com /api/chat retornando 503)."""
    global _client
    if not ANTHROPIC_KEY:
        return None
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_KEY)
    return _client


# ============== TOOLS ==============
TOOLS = [
    {
        "name": "search_flights",
        "description": (
            "Busca trechos SOLTOS (one-way) de ida ou de volta nos dados raspados. "
            "Use somente quando o usuario explicitamente pedir 'so ida', 'so trecho', "
            "'one-way'. Para perguntas gerais sobre 'viagem', 'passagem', 'ir pra X', "
            "use find_trip_combinations (ida+volta)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origins": {"type": "array", "items": {"type": "string"},
                            "description": "Lista de IATAs de origem (ex: ['GRU','GIG'])"},
                "dests": {"type": "array", "items": {"type": "string"},
                          "description": "Lista de IATAs de destino"},
                "date_start": {"type": "string", "description": "ISO YYYY-MM-DD inicio do range"},
                "date_end": {"type": "string", "description": "ISO YYYY-MM-DD fim do range"},
                "max_miles": {"type": "integer", "description": "Maximo de milhas (em milhares, ex: 50)"},
                "program": {"type": "string", "description": "G3 (Smiles), TP (TAP). Default: todos"},
                "cabin": {"type": "string", "description": "economy/business/first"},
                "direction_filter": {"type": "string", "enum": ["outbound", "inbound"],
                                     "description": "outbound = ida (origem -> destino), inbound = volta"},
                "limit": {"type": "integer", "description": "Max resultados (default 30)"},
            },
        },
    },
    {
        "name": "find_trip_combinations",
        "description": (
            "Busca PARES (ida + volta) da MESMA rota. ESTA EH A FUNCAO PADRAO para "
            "perguntas sobre 'passagem', 'viagem', 'ir pra X' - pessoas planejam "
            "viagens completas. Cada resultado eh um pacote {outbound_date, "
            "inbound_date, total_miles}. Filtros de duracao da viagem (min_trip_days, "
            "max_trip_days) ajudam a achar combinacoes plausiveis (ex: 3-15 dias)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origins": {"type": "array", "items": {"type": "string"}},
                "dests": {"type": "array", "items": {"type": "string"}},
                "date_start": {"type": "string", "description": "ISO YYYY-MM-DD inicio"},
                "date_end": {"type": "string", "description": "ISO YYYY-MM-DD fim - aplicado a IDA"},
                "max_total_miles": {"type": "integer",
                                    "description": "Limite TOTAL ida+volta em milhares (ex: 100)"},
                "program": {"type": "string", "description": "G3 (Smiles), TP (TAP)"},
                "cabin": {"type": "string", "description": "economy/business/first"},
                "min_trip_days": {"type": "integer",
                                  "description": "Minimo de dias da viagem (default 3)"},
                "max_trip_days": {"type": "integer",
                                  "description": "Maximo de dias (default 30)"},
                "limit": {"type": "integer", "description": "Max combinacoes (default 20)"},
            },
        },
    },
    {
        "name": "region_to_iatas",
        "description": (
            "Converte nome de regiao em lista de IATAs. Use ANTES de search/find quando "
            "user fala 'Sudeste', 'Nordeste', 'Europa', 'EUA', 'Caribe', 'America do Sul'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome da regiao"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "holiday_date_range",
        "description": (
            "Range de datas de um feriado BR. Use quando user mencionar 'Carnaval', "
            "'Semana Santa', 'Tiradentes', 'Dia do Trabalho', 'Corpus Christi', "
            "'Independencia', 'Finados', 'Proclamacao da Republica', 'Natal', 'Reveillon'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "holiday": {"type": "string", "description": "Nome do feriado (ex: 'carnaval')"},
                "year": {"type": "integer", "description": "Ano (2026, 2027, 2028)"},
            },
            "required": ["holiday", "year"],
        },
    },
    {
        "name": "month_date_range",
        "description": "Converte ('julho', 2026) em ('2026-07-01', '2026-07-31').",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Nome do mes em PT-BR"},
                "year": {"type": "integer"},
            },
            "required": ["month", "year"],
        },
    },
    {
        "name": "list_holidays",
        "description": "Lista feriados BR. Filtros: year, from_date (ignora feriados que ja passaram).",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "from_date": {"type": "string", "description": "ISO YYYY-MM-DD - so feriados >= esta data"},
            },
        },
    },
    {
        "name": "list_available_scopes",
        "description": (
            "Sumario do que tem nos dados: total de rotas, programas disponiveis, "
            "origens com dados. Use SEMPRE no comeco se a pergunta for vaga ou "
            "se voce nao sabe se a rota existe."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_route_details",
        "description": (
            "Detalhe completo de UMA rota especifica (route_id). Retorna lista achatada "
            "de outbound + inbound com datas e precos. Use pra mostrar TODAS as datas "
            "disponiveis de uma rota especifica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {"type": "string"},
                "max_miles": {"type": "integer", "description": "Filtrar so datas abaixo desse limite"},
            },
            "required": ["route_id"],
        },
    },
]


def execute_tool(name: str, args: Dict[str, Any]) -> Any:
    """Dispatcher: executa a tool e retorna o resultado (vai serializar a JSON)."""
    if name == "search_flights":
        return data_loader.search_flights(
            origins=args.get("origins"),
            dests=args.get("dests"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            max_miles=args.get("max_miles"),
            program=args.get("program"),
            cabin=args.get("cabin"),
            direction_filter=args.get("direction_filter", "outbound"),
            limit=args.get("limit", 30),
        )
    if name == "find_trip_combinations":
        return data_loader.find_trip_combinations(
            origins=args.get("origins"),
            dests=args.get("dests"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            max_total_miles=args.get("max_total_miles"),
            program=args.get("program"),
            cabin=args.get("cabin"),
            min_trip_days=args.get("min_trip_days", 3),
            max_trip_days=args.get("max_trip_days", 30),
            limit=args.get("limit", 20),
        )
    if name == "region_to_iatas":
        return regions.region_to_iatas(args.get("name", ""))
    if name == "holiday_date_range":
        rng = regions.holiday_date_range(args.get("holiday", ""), args.get("year", 0))
        if not rng:
            return {"error": "feriado/ano nao encontrado"}
        return {"start": rng[0], "end": rng[1]}
    if name == "month_date_range":
        rng = regions.month_date_range(args.get("month", ""), args.get("year", 0))
        if not rng:
            return {"error": "mes/ano nao encontrado"}
        return {"start": rng[0], "end": rng[1]}
    if name == "list_holidays":
        return regions.list_holidays(year=args.get("year"), from_date=args.get("from_date"))
    if name == "list_available_scopes":
        return data_loader.list_available_scopes()
    if name == "get_route_details":
        out = data_loader.get_route_details(args.get("route_id"), args.get("max_miles"))
        return out or {"error": "rota nao encontrada"}
    return {"error": f"tool desconhecida: {name}"}


# ============== SYSTEM PROMPT ==============
def build_system_prompt() -> str:
    today = datetime.now().date()
    today_iso = today.isoformat()
    current_year = today.year
    return f"""Voce eh o assistente do Buscador Automatico Smiles - sistema que rastreia
disponibilidade de PASSAGENS COM MILHAS no AwardTool (programas Smiles GOL e TAP).
Hoje e {today_iso}. Ano atual: {current_year}.

DADOS:
Voce TEM ACESSO aos dados raspados via tools. So usa esses dados.
NUNCA inventa precos, datas ou rotas. Se nao houver dados, diga que nao tem.

REGRA DE OURO - quando o usuario pergunta sobre "passagem", "viagem", "ir pra X"
SEM dizer que eh so ida:
  SEMPRE use find_trip_combinations.
  NUNCA use search_flights nesses casos.
Pessoas planejam VIAGENS (ida + volta). Mesmo perguntas vagas como "qual a melhor
passagem pra Buenos Aires em julho" devem ser tratadas como ida+volta.

Use search_flights APENAS quando o usuario explicitamente diz: "so ida",
"so o trecho", "one-way", "so a volta", "so de ida".

REGIOES:
- Quando user fala uma regiao ("Sudeste", "Europa", "Caribe", "America do Sul",
  "Nordeste", "EUA", etc), chame region_to_iatas PRIMEIRO e use o array como
  filtro em origins ou dests.

FERIADOS:
- Carnaval, Semana Santa, Tiradentes, Independencia, Natal, Reveillon, etc.
- Use holiday_date_range pra pegar o range exato.
- Se user nao especificar ano, prefira o ANO ATUAL ou PROXIMO ANO se a data
  ja passou. Use list_holidays(from_date={today_iso}) pra saber o que ainda vem.

MESES:
- "julho", "agosto" - use month_date_range pra pegar o range.

PRECOS:
- Tudo em MILHAS (programas Smiles e TAP nao usam moeda).
- Mostre como "92K milhas" ou "92 mil milhas".
- Para ida+volta de find_trip_combinations: total_miles ja eh a soma dos 2
  trechos. Apresente como pacote: "ida 12/05, volta 22/05 - 92K total (50K + 42K)".
- NUNCA mostre o pricing_type pro usuario - eh metadata interna.

NUNCA SUGIRA:
- Datas no passado (hoje eh {today_iso}, antes disso eh passado).
- Origens ou destinos que nao aparecem em list_available_scopes.
- Numeros que voce nao tirou de uma tool.

FORMATO DE RESPOSTA:
- Use markdown com **negrito** e listas.
- 3-8 melhores opcoes, ordenadas por menor preco.
- Por linha: **Origem -> Destino** | ida DD/MM | volta DD/MM (X dias) | **TotalK milhas**
- Destaque o campeao (mais barato) com emoji 🥇.
- Sugira 2-3 perguntas relacionadas no final ("Quer ver opcoes em junho tambem?").

PRINCIPAL:
- Responda direto. Sem "claro!", "com prazer!", apresentacao desnecessaria.
- Se a pergunta for vaga, faca 1 pergunta de clarificacao curta.
- Se nao tem dados pra responder, diga "nao tenho dados de [rota] ainda" e
  sugira uma alternativa proxima dos dados disponiveis.
"""


# ============== MODELS ==============
class ChatMessage(BaseModel):
    role: str  # 'user' | 'assistant'
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []
    scope_hint: Optional[str] = None  # opcional, futuro


class ToolCallLog(BaseModel):
    name: str
    input: Dict[str, Any]
    result_preview: str  # primeiros 200 chars


class ChatResponse(BaseModel):
    reply: str
    tool_calls: List[ToolCallLog] = []


# ============== CHAT LOOP ==============
def _truncate(s: str, max_len: int = TOOL_RESULT_MAX_CHARS) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"\n[... truncado em {max_len} chars]"


def run_chat(message: str, history: List[ChatMessage]) -> ChatResponse:
    client = _get_client()
    if client is None:
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY nao configurada. Setar via env var no container.",
        )

    # Construir messages list
    msgs: List[Dict[str, Any]] = []
    for h in history[-20:]:  # ultimos 20 turnos
        msgs.append({"role": h.role, "content": h.content})
    msgs.append({"role": "user", "content": message})

    system = build_system_prompt()
    tool_log: List[ToolCallLog] = []

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=msgs,
            )
        except Exception as e:
            raise HTTPException(500, f"Erro chamando Claude: {e}")

        if resp.stop_reason == "end_turn":
            # extrair texto final
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            return ChatResponse(reply="\n".join(texts).strip(), tool_calls=tool_log)

        if resp.stop_reason == "tool_use":
            # processar tools
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]

            # add assistant message com tool_use blocks
            msgs.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for tu in tool_uses:
                try:
                    result = execute_tool(tu.name, tu.input)
                    serialized = json.dumps(result, ensure_ascii=False, default=str)
                    serialized = _truncate(serialized)
                except Exception as e:
                    serialized = json.dumps({"error": str(e)})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": serialized,
                })
                tool_log.append(ToolCallLog(
                    name=tu.name,
                    input=dict(tu.input),
                    result_preview=serialized[:200],
                ))
            msgs.append({"role": "user", "content": tool_results})
            continue

        # Outras stop_reasons: max_tokens etc
        texts = [b.text for b in resp.content if hasattr(b, "text")]
        text = "\n".join(texts).strip()
        if not text:
            text = f"[stop_reason={resp.stop_reason} sem texto]"
        return ChatResponse(reply=text, tool_calls=tool_log)

    return ChatResponse(
        reply="Desculpa, atingi o limite de iteracoes de tools. Tenta reformular a pergunta?",
        tool_calls=tool_log,
    )


# ============== APP ==============
app = FastAPI(title="Buscador Smiles - Assistente IA")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/assistente/health")
async def health():
    info = data_loader.list_available_scopes()
    return {
        "status": "ok",
        "model": MODEL,
        "key_configured": bool(ANTHROPIC_KEY),
        "data": info,
    }


@app.post("/api/assistente/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(400, "mensagem vazia")
    return run_chat(req.message, req.history)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
