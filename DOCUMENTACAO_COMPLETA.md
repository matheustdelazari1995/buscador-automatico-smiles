# Buscador Automatico Smiles - Documentacao Completa

> **Versao**: 2.0 (Abril 2026) — arquitetura multi-conta, persistencia, VPS-ready
> **Stack**: Python 3.11 + FastAPI + Playwright + WebSocket + Docker
> **Status**: Em producao local (Mac), pronto pra deploy em VPS

---

## Indice

1. [Visao geral](#1-visao-geral)
2. [Arquitetura](#2-arquitetura)
3. [Estrutura de arquivos](#3-estrutura-de-arquivos)
4. [Fluxos e comportamentos](#4-fluxos-e-comportamentos)
5. [API REST](#5-api-rest)
6. [WebSocket](#6-websocket)
7. [Formato dos dados (JSON)](#7-formato-dos-dados-json)
8. [Frontend (dashboard)](#8-frontend-dashboard)
9. [Como rodar localmente](#9-como-rodar-localmente)
10. [Deploy em VPS](#10-deploy-em-vps)
11. [Troubleshooting + bugs conhecidos](#11-troubleshooting--bugs-conhecidos)
12. [Evolucao do projeto](#12-evolucao-do-projeto)

---

## 1. Visao geral

### O que faz

Sistema automatizado de scraping do [AwardTool](https://www.awardtool.com) que busca datas com disponibilidade de passagens aereas por milhas, apresenta num dashboard web com filtros, e envia resumos filtrados por valor via WhatsApp (Evolution API).

### Principais features

- **Multi-conta com workers paralelos** — cada conta AwardTool = 1 Chrome com perfil proprio (cookies/login persistentes), buscando rotas em paralelo. Workers independentes.
- **Persistencia em JSON** — rotas, resultados, contas e estado do sistema salvos em disco. Reiniciar o servidor nao perde dados.
- **Retomada de onde parou** — se o AwardTool bloquear uma conta no meio da busca (mes 6 de 12), o sistema salva o progresso parcial e continua do mes 6 quando desbloquear.
- **Detector de bloqueio** — reconhece o popup "Searching Too Frequently" do AwardTool + keywords genericas ("rate limit", "too many requests", etc.). Conta bloqueada pausa 10 minutos sozinha, as outras continuam.
- **Pausa preventiva anti-bot** — a cada 8 meses buscados (contador global por conta), pausa 60s. Reduz drasticamente bloqueios.
- **Cooldown entre rotas** — 10 min entre rotas na mesma conta pra evitar padrao de bot. Interrupcivel via botao "Pular cooldown".
- **Pause/Resume global** — botao no header pausa/retoma todas as contas. Estado persistente (sobrevive a reinicios).
- **Filtros avancados nos resultados** — colapsavel, filtrar por origem/destino/mes, ordenar por menor preco.
- **WhatsApp com filtro dinamico** — no momento de enviar, user escolhe o limite de milhas. A mensagem sai so com datas abaixo desse limite.
- **Crash recovery** — ao iniciar, o sistema reseta contas e rotas com status "searching" (inconsistente) pra evitar travamento.
- **Docker-ready** — Dockerfile com Xvfb + noVNC pra rodar em VPS. Login remoto das contas via browser.

### Tecnologias

| Camada | Tech |
|--------|------|
| Backend | Python 3.11 + FastAPI + Uvicorn |
| Scraping | Playwright + Chrome real (channel="chrome") |
| Realtime | WebSocket (FastAPI nativo) |
| Storage | JSON (routes.json, results.json, accounts.json, system_state.json) |
| Frontend | HTML/CSS/JS single file (sem framework) |
| WhatsApp | Evolution API (REST, aiohttp) |
| Deploy | Docker + docker-compose + Xvfb + noVNC + supervisor |

---

## 2. Arquitetura

### Diagrama geral

```
┌──────────────────────────────────────────────────────────────┐
│                   Frontend (browser)                         │
│                                                              │
│   Tabs: Rotas | Resultados | Contas                          │
│   WebSocket bidirecional com backend                         │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            │ HTTP + WebSocket
                            ▼
┌──────────────────────────────────────────────────────────────┐
│               FastAPI server (porta 8001)                    │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐    │
│   │  Endpoints REST                                     │    │
│   │  /api/routes, /api/accounts, /api/results, etc.     │    │
│   └─────────────────────────────────────────────────────┘    │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐    │
│   │  WebSocket /ws (broadcast de progresso)             │    │
│   └─────────────────────────────────────────────────────┘    │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐    │
│   │  asyncio.Queue (fila centralizada de rotas)         │    │
│   └─────────────────────────────────────────────────────┘    │
│                            │                                 │
│          ┌─────────────────┼─────────────────┐               │
│          ▼                 ▼                 ▼               │
│   ┌────────────┐    ┌────────────┐    ┌────────────┐         │
│   │ Worker c1  │    │ Worker c2  │    │ Worker cN  │         │
│   │            │    │            │    │            │         │
│   │ Engine:    │    │ Engine:    │    │ Engine:    │         │
│   │ Chrome     │    │ Chrome     │    │ Chrome     │         │
│   │ perfil c1  │    │ perfil c2  │    │ perfil cN  │         │
│   └──────┬─────┘    └──────┬─────┘    └──────┬─────┘         │
└─────────│─────────────────│─────────────────│───────────────┘
          │                 │                 │
          ▼                 ▼                 ▼
┌──────────────────────────────────────────────────────────────┐
│               AwardTool.com (scraping)                        │
└──────────────────────────────────────────────────────────────┘
                            │
                            │ resultados processados
                            ▼
┌──────────────────────────────────────────────────────────────┐
│        Evolution API (envio manual pra WhatsApp)             │
└──────────────────────────────────────────────────────────────┘
```

### Como os componentes conversam

1. **Frontend** cadastra rota via POST /api/routes. Backend persiste em `routes.json`.
2. **Frontend** clica "Iniciar busca" -> POST /api/routes/enqueue-all. Backend coloca IDs na `search_queue` (asyncio.Queue).
3. **Cada worker** (1 por conta) faz loop infinito:
   - Pega proxima rota da fila
   - Reivindica (muda status pra "searching")
   - Chama `engine.search_route()` (Playwright abre Chrome, busca mes a mes)
   - A cada mes buscado, broadcast de progress via WebSocket
   - Ao terminar, salva resultado em `results.json` e broadcast route_completed
   - Entra em cooldown de 10 min antes de pegar proxima rota
4. **Frontend** recebe broadcasts e atualiza UI em tempo real.
5. **User** clica "Enviar pro WhatsApp" no resultado -> POST /api/routes/{id}/send-whatsapp com filtro de preco -> backend monta mensagem e envia via Evolution API.

### Estado em memoria vs disco

| Item | Em memoria | Em disco |
|------|------------|----------|
| Rotas cadastradas | routes_store.routes | routes.json |
| Resultados | routes_store.results | results.json |
| Contas | accounts_store.accounts | accounts.json |
| Estado pause/resume | system_state.paused | system_state.json |
| Fila de rotas | queue_items + asyncio.Queue | Nao (zera no restart) |
| Workers ativos | worker_tasks dict | Nao |
| Engines Chrome | engines dict | Nao |
| Cooldown events | cooldown_skip_events dict | Nao |

**Crash recovery no startup**: se alguma conta estava "searching" ou rota estava "searching" antes do crash, eh resetado pra "idle"/"pending".

---

## 3. Estrutura de arquivos

```
buscador-automatico-smiles/
├── server.py                      # FastAPI backend (21KB)
├── search_engine.py               # Playwright scraper + format WhatsApp (21KB)
├── routes_store.py                # Persistencia de rotas+resultados (5KB)
├── accounts_store.py              # Persistencia de contas (4KB)
├── system_state.py                # Flag paused persistente (1KB)
├── login_helper.py                # Script pra fazer login manual em conta
│
├── static/
│   └── index.html                 # Dashboard completo (single-page)
│
├── config.json                    # Evolution API + rotas schedule (gitignored)
├── config.example.json            # Modelo de config
├── accounts.json                  # Contas cadastradas (gitignored)
├── accounts.example.json          # Modelo
├── routes.json                    # Rotas cadastradas (gitignored, auto-gerado)
├── results.json                   # Resultados das buscas (gitignored, auto-gerado)
├── system_state.json              # {"paused": bool} (gitignored, auto-gerado)
│
├── requirements.txt               # playwright, fastapi, uvicorn, websockets, aiohttp
├── setup.sh                       # Script de setup local
├── .env.example                   # Variaveis de ambiente
├── .gitignore                     # Ignora venv, profiles, dados locais
│
├── Dockerfile                     # Com Chrome + Xvfb + noVNC + supervisor
├── docker-compose.simple.yml      # Deploy simples (so app, sem nginx)
├── docker-compose.prod.yml        # Deploy completo (app + nginx + certbot)
├── deploy/
│   ├── nginx.conf                 # Reverse proxy + HTTPS + basic auth
│   ├── supervisord.conf           # Orquestra Xvfb, x11vnc, noVNC, app
│   └── entrypoint.sh              # Inicializacao do container
│
├── .browser-profile-conta1/       # Perfil Chrome da conta (gitignored)
├── .browser-profile-conta2/       # ... pra cada conta
│
├── DEPLOY_VPS.md                  # Guia passo-a-passo de deploy
├── DOCUMENTACAO_COMPLETA.md       # Este arquivo
├── README.md
└── venv/                          # Virtual env Python (gitignored)
```

### Responsabilidade de cada arquivo

- **server.py**: API REST + WebSocket + orquestracao dos workers. Contem o worker por conta com todo ciclo de vida (pegar rota, processar, lidar com bloqueios, cooldown).
- **search_engine.py**: classe `AwardToolSearchEngine` com `.start()`, `.search_route()`, detector de bloqueio, pausa preventiva. Funcoes auxiliares: `format_result_text`, `send_whatsapp`, `get_min_price`. Constantes de IATA->cidade, programas, Brasil.
- **routes_store.py**: classe `RoutesStore` — CRUD de rotas e resultados em JSON. Thread-safe (asyncio.Lock).
- **accounts_store.py**: classe `AccountsStore` — CRUD de contas. Status: idle/searching/blocked/cooldown/disabled.
- **system_state.py**: classe `SystemState` — flag paused persistente.
- **login_helper.py**: standalone. Roda `python login_helper.py conta2` pra abrir Chrome no perfil conta2 e dar 2 min pro user logar.
- **static/index.html**: tudo em 1 arquivo. 3 tabs, WebSocket, filtros, estado de cards expandidos. Sem build step.

---

## 4. Fluxos e comportamentos

### 4.1 Ciclo de vida de uma rota

```
[CADASTRADA]
   │ POST /api/routes
   ▼
 pending ─────► [Enqueue]
               │ POST /api/routes/enqueue-all
               │ ou /api/routes/{id}/enqueue
               ▼
            searching
               │ worker pega da fila
               │ Playwright abre Chrome
               │ busca mes a mes
               ▼
          ┌────┴────┐
          │         │
  completed      error / blocked
          │         │
          │         ├──► [Erro normal] status=error, user clica Refazer
          │         │
          │         └──► [AwardTool bloqueou]
          │              - salva parcial no results.json (is_partial=true)
          │              - conta pausa 10min (blocked_until)
          │              - apos 10min, mesmo worker retoma DESSA rota
          │              - engine pula meses ja feitos, continua do 6 (ex)
          │              - se conseguir, completa. Senao, pausa de novo.
          │
          ▼
      [RESULTADO SALVO em results.json]
          │
          │ User vai na aba Resultados
          │ Escolhe limite de milhas no campo
          │ Clica "Enviar pro WhatsApp"
          ▼
      POST /api/routes/{id}/send-whatsapp
      {"max_price_k": 45}
          │
          │ Backend monta mensagem filtrada
          │ Envia via Evolution API
          ▼
      [WHATSAPP ENVIADO]
      whatsapp_sent_at preenchido na rota
```

### 4.2 Multi-conta (workers paralelos)

- Cada conta cadastrada em `accounts.json` tem 1 worker em `asyncio.Task`.
- Cada worker tem seu proprio `AwardToolSearchEngine` (1 Chrome com perfil `.browser-profile-contaX/`).
- Todos consomem da mesma `search_queue` (fila de IDs de rotas).
- Se Conta 2 bloqueia, SO ela pausa 10min. Contas 1, 3, 4 continuam pegando rotas.
- Se user desativa uma conta (botao Desativar), o worker termina no proximo loop (nao interrompe rota em andamento).

### 4.3 Detector de bloqueio

Em `search_engine.py`:

```python
BLOCK_DETECTION_KEYWORDS = [
    "searching too frequently",           # Popup AwardTool
    "excessive number of searches",
    "performed an excessive number",
    "please wait for a few minutes",
    "wait for a few minutes and try again",
    "too many requests",                  # Genericas
    "rate limit",
    "try again later",
    "temporarily blocked",
    "access denied",
    "muitas buscas",
    "limite de buscas",
]
SUSPECT_BLOCK_AFTER_EMPTY = 5   # 5 meses seguidos com 0 datas = suspeita
```

Apos cada `page.goto`, checa `document.body.innerText.toLowerCase()` por cada keyword. Se achar, levanta `AwardToolBlocked` com os meses ja completados.

### 4.4 Pausa preventiva anti-bot

```python
SEARCHES_BEFORE_PAUSE = 8    # Apos 8 meses buscados
LONG_PAUSE_SECONDS = 60      # Pausa 60 segundos
```

O contador `self._searches_since_pause` e POR ENGINE (conta), persiste entre rotas. Apos cada mes bem-sucedido, incrementa. Quando chega a 8:
1. Callback `pause_cb(60)` -> broadcast "preventive_pause" -> banner amarelo no frontend
2. `await asyncio.sleep(60)`
3. Callback `pause_cb(0)` -> broadcast "preventive_resumed" -> banner some
4. Contador zera, continua

### 4.5 Retomada de onde parou

Ao bloquear, o engine:
```python
raise AwardToolBlocked(
    "Bloqueio detectado: ...",
    partial_direction=results,  # dict {month_name: [days]}
)
```

Quando chega ao `search_route`:
```python
except AwardToolBlocked as e:
    outbound.update(e.partial_direction or {})
    e.outbound = outbound   # anexa o que ja foi feito
    e.inbound = inbound
    raise
```

Worker captura e salva como parcial:
```python
await routes_store.save_partial_result(route_id, partial_result)
# marca is_partial=true na rota
```

Proxima vez que essa rota for processada, `search_route` recebe `existing_result=partial`. Filtra:
```python
outbound_months_to_do = [m for m in months if m["name"] not in existing_outbound]
```

So busca os que faltam. Ao terminar, merge com o parcial e salva resultado completo.

**Importante**: mesmo meses que retornaram 0 datas sao gravados no `results` dict (como `[]`), pra que `m["name"] not in existing_outbound` funcione corretamente ao retomar.

### 4.6 Cooldown entre rotas (interrupcivel)

Apos cada rota COMPLETADA com sucesso:
```python
cooldown_skip_event.clear()
try:
    await asyncio.wait_for(
        cooldown_skip_event.wait(),
        timeout=DELAY_BETWEEN_ROUTES,  # 600s = 10 min
    )
    # Event foi setado -> user clicou "Pular cooldown"
except asyncio.TimeoutError:
    # Cooldown normal terminou
    pass
```

Endpoint `POST /api/accounts/{id}/skip-cooldown` chama `event.set()`.

### 4.7 Pause/Resume global

Worker tem essa checagem no topo do loop:
```python
if system_state.is_paused():
    await asyncio.sleep(2)
    continue
```

Se `is_paused()` e True, nao pega novas rotas. Rotas em andamento TERMINAM normalmente (nao sao interrompidas).

Flag persistida em `system_state.json`. Se o processo do servidor morrer (sleep do Mac, por exemplo) e voltar, continua pausado.

### 4.8 Priorizar retomada da mesma rota

Quando bloqueia:
```python
pending_retry_route_id = route_id
# NAO coloca de volta no search_queue
```

No proximo loop do mesmo worker (apos 10min blocked):
```python
if pending_retry_route_id is not None:
    route_id = pending_retry_route_id  # usa direto
    pending_retry_route_id = None
else:
    route_id = await search_queue.get()
```

Assim a mesma conta retoma a mesma rota, ao inves de pular pra proxima da fila.

### 4.9 Filtros e ordenacao na aba Resultados

Client-side em JavaScript:
- Cards colapsados por padrao. Click pra expandir (estado em `expandedResults: Set`).
- `resultFilterOrigin`, `resultFilterDest` — substring match
- `resultFilterMonth` — se setado, usa `getMinPriceForMonth(result, monthName)` pra calcular "menor preco naquele mes especifico"
- `resultSortMode`: default / price-asc / price-desc / route-asc

Quando filtro de mes ativo, cada card mostra "Menor em Julho: 17K" ao inves do menor geral.

### 4.10 WhatsApp com filtro dinamico

Resultado salvo tem TODAS as datas com seus precos. Sem filtro na hora da busca.

No momento de enviar pro WhatsApp:
- User escolhe limite no input (default: menor valor encontrado)
- POST com `{"max_price_k": 45}`
- Backend chama `format_result_text(result, max_price_filter=45)`
- Funcao filtra as datas: `filtered = [d for d in days if d["price"] <= max_price]`
- Mensagem sai apenas com datas abaixo de 45K

---

## 5. API REST

### System state

#### `GET /api/system/state`
Retorna: `{"paused": bool}`

#### `POST /api/system/pause`
Pausa o sistema globalmente. Workers param de pegar novas rotas.
Retorna: `{"ok": true, "paused": true}`

#### `POST /api/system/resume`
Retoma o sistema.
Retorna: `{"ok": true, "paused": false}`

### Accounts

#### `GET /api/accounts`
Lista todas as contas.
```json
{"accounts": [
  {
    "id": "conta1",
    "name": "Conta Principal",
    "profile_dir": ".browser-profile-conta1",
    "enabled": true,
    "notes": "Login ja feito",
    "created_at": "2026-04-19T17:39:15",
    "status": "idle",              // idle|searching|blocked|cooldown|disabled
    "current_route_id": null,
    "blocked_until": null,          // epoch timestamp ou null
    "last_error": null
  }
]}
```

#### `POST /api/accounts`
Body: `{"id": "conta2", "name": "Conta 2", "profile_dir": ".browser-profile-conta2", "notes": "..."}`
Cria conta e inicia worker.

#### `DELETE /api/accounts/{account_id}`
Remove conta. Worker e parado. Perfil Chrome no disco NAO e deletado.

#### `POST /api/accounts/{account_id}/toggle`
Alterna enabled. Se desativa, worker para. Se reativa, worker inicia.

#### `POST /api/accounts/{account_id}/skip-cooldown`
Interrompe cooldown de 10min. Worker pega proxima rota imediatamente.

### Routes

#### `GET /api/routes`
```json
{
  "routes": [
    {
      "id": "30cac2c3",
      "origin": "VIX",
      "dest": "CWB",
      "program": "G3",
      "cabin": "economy",           // economy|business|first
      "direction": "roundtrip",     // roundtrip|outbound|inbound
      "months": null,               // null=ano todo ou [{year:2026, month:7}, ...]
      "status": "completed",        // pending|searching|completed|error|blocked
      "created_at": "2026-04-19T17:11:03",
      "last_searched_at": "2026-04-19T17:21:49",
      "last_error": null,
      "whatsapp_sent_at": "2026-04-19T17:39:37",
      "is_partial": false,
      "has_result": true,           // flag computada
      "min_price_k": 17             // flag computada (menor preco geral)
    }
  ],
  "queue": ["route_id1", "route_id2"]   // ordem da fila
}
```

#### `POST /api/routes`
Body: `{"origin": "VIX", "dest": "REC", "program": "TP", "cabin": "economy", "direction": "roundtrip", "months": null}`
Cria rota. Status inicial: pending.

#### `DELETE /api/routes/{route_id}`
Remove rota. Se tiver resultado associado, tambem apaga.

#### `POST /api/routes/{route_id}/enqueue`
Coloca rota na fila (se nao estiver). Status -> pending.

#### `POST /api/routes/enqueue-all`
Enfileira TODAS as rotas nao-concluidas (pending, error, blocked, parcial).
Pula rotas completed (com resultado).
Retorna: `{"ok": true, "enqueued": 5, "skipped_completed": 10}`

#### `POST /api/routes/enqueue-all-force`
Enfileira TODAS, APAGANDO resultados existentes.
Usar com cuidado. Confirmado via confirm() no frontend.

#### `POST /api/routes/{route_id}/retry`
Reseta rota (apaga resultado parcial ou completo) e enfileira.

### Results + WhatsApp

#### `GET /api/results/{route_id}`
Retorna resultado da rota:
```json
{
  "origin": "VIX",
  "dest": "CWB",
  "program": "G3",
  "cabin": "economy",
  "direction": "roundtrip",
  "outbound": {
    "Maio 2026": [{"day": "12", "price": 17}, {"day": "20", "price": 22}],
    "Junho 2026": [...]
  },
  "inbound": {...},
  "searched_at": "19/04/2026 17:21"
}
```

#### `POST /api/routes/{route_id}/send-whatsapp`
Body: `{"max_price_k": 45}` (opcional, default = null = sem filtro)
Envia mensagem formatada pro WhatsApp via Evolution API.
Retorna: `{"ok": true, "sent": true}`

---

## 6. WebSocket

Endpoint: `/ws`

Ao conectar, servidor envia snapshot inicial:
```json
{
  "type": "state",
  "routes": [...],
  "accounts": [...],
  "queue": [...],
  "paused": false
}
```

### Eventos broadcast

| Tipo | Quando | Payload (exemplo) |
|------|--------|---------|
| `route_added` | Nova rota cadastrada | `{type, route}` |
| `route_removed` | Rota deletada | `{type, route_id}` |
| `route_enqueued` | Rota entrou na fila | `{type, route_id}` |
| `batch_enqueued` | enqueue-all chamado | `{type, count}` |
| `route_status` | Status da rota mudou | `{type, route_id, status, account_id}` |
| `progress` | Durante busca (cada mes) | `{type, route_id, account_id, step, total, percent, message}` |
| `route_completed` | Busca terminou | `{type, route_id, account_id, result}` |
| `route_error` | Erro na busca | `{type, route_id, account_id, error}` |
| `account_added` | Nova conta | `{type, account}` |
| `account_removed` | Conta deletada | `{type, account_id}` |
| `account_updated` | Enable/disable | `{type, account}` |
| `account_blocked` | AwardTool bloqueou | `{type, account_id, route_id, reason, retry_at}` |
| `account_resumed` | Saiu do bloqueio | `{type, account_id}` |
| `account_cooldown` | Iniciou cooldown 10min | `{type, account_id, seconds}` |
| `account_cooldown_end` | Cooldown acabou | `{type, account_id}` |
| `account_cooldown_skipped` | User pulou cooldown | `{type, account_id}` |
| `preventive_pause` | Pausa 60s a cada 8 meses | `{type, account_id, seconds, retry_at}` |
| `preventive_resumed` | Pausa 60s acabou | `{type, account_id}` |
| `system_paused` | User pausou tudo | `{type}` |
| `system_resumed` | User retomou tudo | `{type}` |
| `whatsapp_sent` | WhatsApp enviado com sucesso | `{type, route_id}` |

---

## 7. Formato dos dados (JSON)

### routes.json
Lista de rotas cadastradas. Array de objetos como mostrado em /api/routes acima.

### results.json
Dict `{route_id: result_object}`. Cada result contem origin, dest, program, outbound (dict mes->dias), inbound (dict mes->dias), searched_at, direction, cabin.

### accounts.json
Array de objetos: `{id, name, profile_dir, enabled, notes, created_at, status, current_route_id, blocked_until, last_error}`.

### system_state.json
`{"paused": false}`.

### config.json
```json
{
  "evolution_api": {
    "url": "https://evotripse.tripse.com.br",
    "instance": "clarisse",
    "api_key": "SUA_API_KEY",
    "destination": "5527998269572"
  },
  "schedule": { ... rotas agendadas (nao mais usado, pode remover) ... }
}
```

---

## 8. Frontend (dashboard)

Arquivo unico: `static/index.html` (HTML + CSS + JS inline). Servido em `/`.

### Estrutura de tabs

1. **Rotas** — cadastro + tabela com status/progresso
2. **Resultados** — cards colapsaveis + barra de filtros (origem, destino, mes, ordenacao)
3. **Contas** — CRUD + status em tempo real + skip cooldown

### Elementos chave

- **Header**: titulo + botao Pausar/Retomar + status WebSocket (bolinha verde/vermelha)
- **Banners**:
  - Vermelho (`.block`) — AwardTool bloqueou
  - Amarelo (`.preventive`) — pausa de 60s
  - Cinza (`.paused`) — sistema pausado globalmente
- **Tabela de rotas** com colunas: Rota, Filtros, Status, Progresso, Acoes
- **Cards de resultado** colapsaveis com min price chip e WhatsApp button

### State JavaScript (principais variaveis)

```javascript
let routes = [];                  // array de rotas
let results = {};                 // dict route_id -> result
let progressMap = {};             // route_id -> {percent, message, account_id}
let queueList = [];               // ordem da fila
let accounts = [];
let systemPaused = false;

// Filtros da aba Resultados
let resultFilterOrigin = '';
let resultFilterDest = '';
let resultFilterMonth = '';       // "Julho 2026"
let resultSortMode = 'default';
const expandedResults = new Set();  // route_ids expandidos
const resultFilters = {};           // por card: route_id -> max K scolhido
```

### Conexao WebSocket com reconexao automatica

Em `connectWS()`: se desconecta, tenta reconectar a cada 3s ate conseguir.

---

## 9. Como rodar localmente

### Primeira vez (setup)

```bash
cd "buscador-automatico-smiles"

# Criar venv e instalar deps
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium  # opcional, usa Chrome real

# Criar config
cp config.example.json config.json
# editar com sua API key do Evolution
```

### Rodar

```bash
./venv/bin/python server.py
```

Acessa: http://localhost:8001

### Adicionar nova conta

1. Na aba Contas, clica "Adicionar" com um ID unico (ex: `conta2`)
2. Servidor inicia automaticamente o worker + Chrome dessa conta
3. Pra fazer login no AwardTool:
   - Para o servidor: `pkill -f server.py`
   - Roda: `./venv/bin/python login_helper.py conta2`
   - Chrome abre no perfil dessa conta
   - Faz login manual, tem 2 min
   - Fecha Chrome (ou espera timeout)
   - Reinicia servidor

### Comandos uteis

```bash
# Ver estado atual
curl -s http://localhost:8001/api/routes | python3 -m json.tool
curl -s http://localhost:8001/api/accounts | python3 -m json.tool

# Forcar pausa/retomada
curl -X POST http://localhost:8001/api/system/pause
curl -X POST http://localhost:8001/api/system/resume

# Pular cooldown de uma conta
curl -X POST http://localhost:8001/api/accounts/conta1/skip-cooldown

# Enfileirar todas pendentes
curl -X POST http://localhost:8001/api/routes/enqueue-all
```

---

## 10. Deploy em VPS

Ver arquivo `DEPLOY_VPS.md` para guia passo-a-passo completo (Hetzner + Docker + Xvfb + noVNC + HTTPS + Let's Encrypt).

Resumo dos arquivos de deploy:

- **Dockerfile** — Python + Chrome + Xvfb + noVNC + supervisor
- **docker-compose.simple.yml** — so app, expoe portas 8001 e 6080 direto
- **docker-compose.prod.yml** — app + nginx + certbot pra HTTPS
- **deploy/supervisord.conf** — orquestra Xvfb, x11vnc, noVNC, app no container
- **deploy/entrypoint.sh** — cria diretorios e chama supervisord
- **deploy/nginx.conf** — reverse proxy + basic auth + HTTPS

---

## 11. Troubleshooting + bugs conhecidos

### Bugs ja corrigidos (historico pra consulta)

#### 1. Chrome travando no popup de bloqueio
**Sintoma**: Worker fica "searching" indefinidamente, conta consome 70% CPU.
**Causa**: `page.goto()` sem timeout bloqueia eternamente quando ha popup.
**Solucao**: adicionado `timeout=45000` no `page.goto`. Se nao carregar em 45s, desiste desse mes.

#### 2. Rotas em busca simultanea (fantasma)
**Sintoma**: Dashboard mostra 2 rotas buscando ao mesmo tempo com 1 conta so.
**Causa**: Apos bloqueio, status da rota nao era atualizado pra pending no frontend. O progress antigo ficava "congelado".
**Solucao**: Ao bloquear, broadcast `route_status: pending` e frontend limpa progressMap quando status != searching.

#### 3. Rota fantasma na fila
**Sintoma**: Fila tem uma rota completed que nao deveria estar la. Worker pega, nada acontece.
**Causa**: queue_items out of sync com search_queue apos bloqueios e re-enqueues.
**Solucao**: Worker detecta rotas "completed" ou "searching" e pula. Tambem: em caso de AwardToolBlocked, `pending_retry_route_id` em vez de re-enqueue.

#### 4. Worker pulando pra proxima rota apos bloqueio
**Sintoma**: Bloqueou no meio de EZE. Apos 10min, pegou MVD (proxima da fila) em vez de continuar EZE.
**Causa**: `search_queue.put(route_id)` coloca no FINAL da fila.
**Solucao**: Variavel local `pending_retry_route_id` no worker. Mesma conta retoma mesma rota apos unblock.

#### 5. Frontend zerado (tudo em branco)
**Sintoma**: Dashboard mostra 0 rotas, 0 resultados, WebSocket desconectado.
**Causa**: `const MONTH_NAMES_PT` declarado DEPOIS do `initMonthFilterDropdown()` ser chamado. Temporal dead zone do `const` travou o script.
**Solucao**: Moveu chamada `initMonthFilterDropdown()` pra fim do script (junto com `connectWS()`).

#### 6. Cooldown de 10min parando tudo
**Sintoma**: Apos rota completar, conta fica 10min sem fazer nada.
**Causa**: `asyncio.sleep(600)` nao interrupcivel.
**Solucao**: `asyncio.wait_for(cooldown_skip_event.wait(), timeout=600)`. User pode clicar "Pular cooldown" e o sleep termina antes.

#### 7. Estado inconsistente apos crash
**Sintoma**: Conta fica "searching" eternamente depois de matar o servidor no meio.
**Causa**: Sem crash recovery.
**Solucao**: Startup reseta contas/rotas com status "searching" pra "idle"/"pending".

### Problemas comuns

#### "ModuleNotFoundError: fastapi"
Usando Python do sistema ao inves do venv. Rodar `./venv/bin/python server.py`.

#### "Browser profile locked"
Outro processo esta usando o mesmo perfil. Fechar Chrome manual ou `pkill -f ".browser-profile-contaX"`.

#### "AwardTool bloqueou logo nas primeiras buscas"
IP datacenter. Precisa de proxy residencial. Ver secao de proxies no DEPLOY_VPS.md (a implementar).

#### "Chrome nao abre (headless=False no VPS)"
Falta Xvfb. Usar Dockerfile que ja tem.

---

## 12. Evolucao do projeto

### Versao 1.0 (inicial)
- 1 conta so
- Busca sequencial
- Scheduler (rotas agendadas no config.json)
- WhatsApp automatico

### Versao 1.5
- Dashboard web com tabs (Rotas / Resultados)
- Cadastro manual de rotas
- WhatsApp manual (botao)
- Filtro de preco dinamico
- Salva todas as datas (nao so abaixo do limite)
- Retomar de onde parou apos bloqueio

### Versao 2.0 (atual)
- Multi-conta com workers paralelos
- Aba Contas
- Pause/Resume global
- Cooldown interrupcivel
- Pausa preventiva anti-bot
- Filtros avancados em Resultados (origem, destino, mes especifico, sort por preco)
- Cards colapsaveis
- Crash recovery
- Dockerfile com Xvfb + noVNC
- Deploy VPS documentado

### Versao 3.0 (planejado)
- **Suporte a proxies** — cada conta com seu proxy (IPRoyal Static Residential)
- **UI de bulk import** — cadastrar 10+ contas via CSV
- **Rodar junto com projeto Google Flights** na mesma VPS
- **Healthcheck de proxies** — testar conectividade antes de ativar conta
- **Monitoring estendido** — alertas, estatisticas de bloqueio

---

## Anexo: Snippets chave do codigo

### Regex que extrai datas+precos do AwardTool

```javascript
// Em search_engine.py JS_EXTRACT_TEMPLATE
() => {
    const text = document.body.innerText;
    const regex = /((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{2}\/\d{2})\nOnly\n([^\n]+)\n([^\n]+)\n([^\n]+)\n([^\n]+)/g;
    const results = [];
    let match;
    while ((match = regex.exec(text)) !== null) {
        const cabin = match[{cabin_index}].trim();  // match[2]=Eco, [3]=PremEco, [4]=Business, [5]=First
        const m = cabin.match(/([\.\d]+)K/);
        if (m) {
            results.push({
                day: match[1].trim().split(' ')[1].split('/')[1],
                price: parseFloat(m[1])
            });
        }
    }
    return { count: results.length, days: results };
}
```

### URL do AwardTool

```
https://www.awardtool.com/flight?flightWay=oneway&pax=1&children=0
  &cabins=Economy%26Premium+Economy%26Business%26First
  &range=true&rangeV2=false
  &from={IATA_ORIGEM}&to={IATA_DESTINO}
  &programs={CODIGO}          // TP (TAP), G3 (Smiles)
  &oneWayRangeStartDate={UNIX_START}
  &oneWayRangeEndDate={UNIX_END}
```

### Inicio do engine (Playwright com Chrome real)

```python
self.context = await self.playwright.chromium.launch_persistent_context(
    browser_profile,
    channel="chrome",               # CRITICO: Chrome real, nao Chromium de teste
    headless=False,
    viewport={"width": 1920, "height": 1080},
    args=[
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ],
)
```

### Formato da mensagem WhatsApp

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

**Autor**: Matheus + Claude
**Ultima atualizacao**: Abril 2026
