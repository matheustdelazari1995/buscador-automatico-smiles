"""
FastAPI server for Buscador Automatico Smiles.
- Multi-conta: cada conta = 1 Chrome com perfil proprio, rodam em paralelo
- Fila centralizada distribui rotas pras contas livres
- Bloqueio por conta: se Conta X bloqueia, so ela pausa 10min
- 2 abas: Rotas (cadastro) e Resultados (WhatsApp manual)
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from search_engine import (
    AwardToolSearchEngine,
    AwardToolBlocked,
    format_result_text,
    send_whatsapp,
    get_min_price,
    test_proxy,
)
from routes_store import RoutesStore
from accounts_store import AccountsStore
from system_state import SystemState


app = FastAPI(title="Buscador Automatico Smiles")

# ===== Config =====
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
BLOCK_PAUSE_SECONDS = 10 * 60  # 10 minutes when an account is blocked
DELAY_BETWEEN_ROUTES = 10 * 60  # 10 minutes between routes on same account


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


# ===== State =====
active_connections = []
routes_store: Optional[RoutesStore] = None
accounts_store: Optional[AccountsStore] = None
engines = {}         # account_id -> AwardToolSearchEngine
worker_tasks = {}    # account_id -> asyncio.Task

# Fila ordenada de rotas. Pode ser reordenada a qualquer momento.
# Workers pegam o PRIMEIRO item. Condition permite aguardar eficientemente
# quando a lista esta vazia e tambem permite notificar apos reordenar.
queue_items = []     # route_ids in queue (ordered)
queue_cond = None    # asyncio.Condition (lock + wait/notify)

system_state: Optional[SystemState] = None
cooldown_skip_events = {}  # account_id -> asyncio.Event (set to skip cooldown)


# ===== Models =====
class RouteIn(BaseModel):
    origin: str
    dest: str
    program: str
    cabin: str = "economy"
    direction: str = "roundtrip"
    months: Optional[List[dict]] = None


class AccountIn(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    profile_dir: Optional[str] = None
    enabled: bool = True
    notes: str = ""
    proxy_server: Optional[str] = None   # e.g. "http://proxy.iproyal.com:12321"
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None


class ProxyUpdateIn(BaseModel):
    proxy_server: Optional[str] = None
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None


class WhatsAppRequest(BaseModel):
    max_price_k: Optional[int] = None


# ===== WebSocket =====
async def broadcast(msg: dict):
    dead = []
    for ws in active_connections:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_connections.remove(ws)


# ===== Worker por conta =====
async def account_worker(account_id: str):
    """One worker per account. Each has its own Chrome engine."""
    account = await accounts_store.get_account(account_id)
    if not account:
        return

    # Build proxy config (only if server is set)
    proxy_cfg = None
    if account.get("proxy_server"):
        proxy_cfg = {"server": account["proxy_server"]}
        if account.get("proxy_user"):
            proxy_cfg["username"] = account["proxy_user"]
            proxy_cfg["password"] = account.get("proxy_pass") or ""

    engine = AwardToolSearchEngine(
        profile_dir=account["profile_dir"],
        account_id=account_id,
        proxy=proxy_cfg,
    )
    engines[account_id] = engine

    # Preventive pause callback (per engine/account)
    async def on_preventive_pause(seconds):
        if seconds > 0:
            retry_at = datetime.fromtimestamp(
                datetime.now().timestamp() + seconds
            ).strftime("%H:%M:%S")
            await broadcast({
                "type": "preventive_pause",
                "account_id": account_id,
                "seconds": seconds,
                "retry_at": retry_at,
            })
        else:
            await broadcast({"type": "preventive_resumed", "account_id": account_id})
    engine.pause_cb = on_preventive_pause

    # Track route to retry FIRST after a block (so we resume the same route
    # instead of moving to the next one in the queue)
    pending_retry_route_id = None

    # Event that can be set externally to skip the inter-route cooldown
    cooldown_skip_event = asyncio.Event()
    cooldown_skip_events[account_id] = cooldown_skip_event

    while True:
        # Check if system is globally paused - if so, don't pick new routes
        if system_state.is_paused():
            await asyncio.sleep(2)
            continue

        # Check if still enabled
        acc = await accounts_store.get_account(account_id)
        if not acc or not acc.get("enabled"):
            await accounts_store.set_status(account_id, "disabled")
            await asyncio.sleep(5)
            continue

        # Check if still blocked
        if acc.get("blocked_until"):
            now_ts = datetime.now().timestamp()
            if now_ts < acc["blocked_until"]:
                await asyncio.sleep(5)
                continue
            else:
                # Unblock and broadcast
                await accounts_store.set_status(account_id, "idle")
                await broadcast({"type": "account_resumed", "account_id": account_id})

        # Decide which route to process next
        if pending_retry_route_id is not None:
            # Resume the route that was blocked (doesn't come from queue)
            route_id = pending_retry_route_id
            pending_retry_route_id = None
        else:
            # Pop the first route from queue_items (ordered list).
            # Wait on queue_cond if queue is empty.
            async with queue_cond:
                # Wait until there's something in the queue (with short timeout
                # so we can re-check pause/enabled/blocked flags periodically)
                if not queue_items:
                    try:
                        await asyncio.wait_for(queue_cond.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                if not queue_items:
                    continue
                route_id = queue_items.pop(0)
            await broadcast({"type": "queue_updated", "queue": list(queue_items)})

        route = await routes_store.get_route(route_id)
        if not route:
            continue

        # Skip if already completed (stale queue entry from duplicate enqueue)
        if route.get("status") == "completed" and not route.get("is_partial"):
            continue

        # Skip if being processed by another worker
        if route.get("status") == "searching":
            continue

        # Claim the route
        await routes_store.update_status(route_id, "searching")
        await accounts_store.set_status(account_id, "searching", current_route_id=route_id)
        await broadcast({
            "type": "route_status",
            "route_id": route_id,
            "status": "searching",
            "account_id": account_id,
        })

        async def progress_cb(step, total, msg):
            pct = round((step / total) * 100)
            await broadcast({
                "type": "progress",
                "route_id": route_id,
                "account_id": account_id,
                "step": step,
                "total": total,
                "percent": pct,
                "message": msg,
            })

        try:
            # Load existing partial result (if any) so engine skips already-done months
            existing = await routes_store.get_result(route_id)
            existing_for_resume = existing if route.get("is_partial") else None

            result = await engine.search_route(
                route["origin"],
                route["dest"],
                route["program"],
                999999,  # no filter - save all prices
                progress_cb=progress_cb,
                selected_months=route.get("months"),
                direction=route.get("direction", "roundtrip"),
                cabin=route.get("cabin", "economy"),
                existing_result=existing_for_resume,
            )
            result["route_id"] = route_id
            result["account_id"] = account_id
            await routes_store.save_result(route_id, result)
            await broadcast({
                "type": "route_completed",
                "route_id": route_id,
                "account_id": account_id,
                "result": result,
            })

            # Delay between routes (on same account) to avoid bot detection.
            # Interruptible: if user clicks "Pular cooldown", the event is set
            # and this sleep ends early.
            await accounts_store.set_status(account_id, "idle")
            await broadcast({
                "type": "account_cooldown",
                "account_id": account_id,
                "seconds": DELAY_BETWEEN_ROUTES,
            })
            cooldown_skip_event.clear()
            try:
                await asyncio.wait_for(
                    cooldown_skip_event.wait(),
                    timeout=DELAY_BETWEEN_ROUTES,
                )
                # Event was set externally - cooldown was skipped
                await broadcast({
                    "type": "account_cooldown_skipped",
                    "account_id": account_id,
                })
            except asyncio.TimeoutError:
                # Normal cooldown ended
                pass
            await broadcast({"type": "account_cooldown_end", "account_id": account_id})

        except AwardToolBlocked as e:
            blocked_ts = datetime.now().timestamp() + BLOCK_PAUSE_SECONDS
            retry_at = datetime.fromtimestamp(blocked_ts).strftime("%H:%M:%S")

            # Save PARTIAL result (months already done) so we can resume later
            partial_result = {
                "origin": route["origin"],
                "dest": route["dest"],
                "program": route["program"],
                "max_price_k": 999999,
                "direction": route.get("direction", "roundtrip"),
                "cabin": route.get("cabin", "economy"),
                "outbound": e.outbound or {},
                "inbound": e.inbound or {},
                "route_id": route_id,
                "account_id": account_id,
                "searched_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
            }
            await routes_store.save_partial_result(route_id, partial_result)

            await accounts_store.set_status(
                account_id, "blocked",
                blocked_until=blocked_ts,
                error=str(e),
            )
            await broadcast({
                "type": "account_blocked",
                "account_id": account_id,
                "route_id": route_id,
                "reason": str(e),
                "retry_at": retry_at,
            })
            # Mark route as pending and set up same-account retry for AFTER the
            # 10-min block. This means this SAME worker resumes THIS SAME route
            # first, instead of moving to the next one in the queue.
            await routes_store.update_status(route_id, "pending")
            await broadcast({
                "type": "route_status",
                "route_id": route_id,
                "status": "pending",
                "account_id": None,
            })
            pending_retry_route_id = route_id
            # NOTE: worker keeps the route locally (pending_retry) and resumes
            # it after unblock. Not re-added to queue_items.

        except Exception as e:
            await routes_store.update_status(route_id, "error", str(e))
            await accounts_store.set_status(account_id, "idle", error=str(e))
            await broadcast({
                "type": "route_error",
                "route_id": route_id,
                "account_id": account_id,
                "error": str(e),
            })

        # Nothing to clean up in queue_items - route was already popped when picked.


async def start_worker_for_account(account_id: str):
    """Start a worker task for an account if not already running."""
    if account_id in worker_tasks and not worker_tasks[account_id].done():
        return
    worker_tasks[account_id] = asyncio.create_task(account_worker(account_id))


async def stop_worker_for_account(account_id: str):
    """Stop worker task for an account."""
    if account_id in worker_tasks:
        worker_tasks[account_id].cancel()
        del worker_tasks[account_id]


# ===== Startup =====
@app.on_event("startup")
async def startup():
    global queue_cond, routes_store, accounts_store, system_state
    queue_cond = asyncio.Condition()
    routes_store = RoutesStore()
    accounts_store = AccountsStore()
    system_state = SystemState()

    # CRASH RECOVERY: reset any accounts stuck in 'searching' to 'idle'
    # and any routes stuck in 'searching' back to 'pending' (they'll need re-queue)
    all_accounts = await accounts_store.list_accounts()
    for acc in all_accounts:
        if acc.get("status") in ("searching", "cooldown"):
            await accounts_store.set_status(acc["id"], "idle")
        # Don't auto-unblock - let the blocked_until timer handle that

    all_routes = await routes_store.list_routes()
    for r in all_routes:
        if r.get("status") == "searching":
            await routes_store.update_status(r["id"], "pending")

    # Start one worker per enabled account
    enabled = await accounts_store.enabled_accounts()
    for acc in enabled:
        await start_worker_for_account(acc["id"])


# ===== Endpoints: System state (pause/resume) =====
@app.get("/api/system/state")
async def get_system_state():
    return {"paused": system_state.is_paused()}


@app.post("/api/system/pause")
async def pause_system():
    await system_state.set_paused(True)
    await broadcast({"type": "system_paused"})
    return {"ok": True, "paused": True}


@app.post("/api/system/resume")
async def resume_system():
    await system_state.set_paused(False)
    await broadcast({"type": "system_resumed"})
    return {"ok": True, "paused": False}


# ===== Endpoints: Accounts =====
@app.get("/api/accounts")
async def list_accounts():
    accounts = await accounts_store.list_accounts()
    return {"accounts": accounts}


@app.post("/api/accounts")
async def add_account(acc: AccountIn):
    new_acc = await accounts_store.add_account(acc.dict())
    if new_acc["enabled"]:
        await start_worker_for_account(new_acc["id"])
    await broadcast({"type": "account_added", "account": new_acc})
    return new_acc


@app.delete("/api/accounts/{account_id}")
async def remove_account(account_id: str):
    await stop_worker_for_account(account_id)
    ok = await accounts_store.remove_account(account_id)
    if not ok:
        raise HTTPException(404, "Account not found")
    await broadcast({"type": "account_removed", "account_id": account_id})
    return {"ok": True}


@app.put("/api/accounts/{account_id}/proxy")
async def update_account_proxy(account_id: str, req: ProxyUpdateIn):
    """Atualiza configuracao de proxy de uma conta.
    IMPORTANTE: a conta precisa ser reiniciada (desativar e reativar) para usar o novo proxy."""
    acc = await accounts_store.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    updated = await accounts_store.update_proxy(
        account_id,
        req.proxy_server,
        req.proxy_user,
        req.proxy_pass,
    )
    await broadcast({"type": "account_updated", "account": updated})
    return updated


@app.post("/api/accounts/{account_id}/test-proxy")
async def test_account_proxy(account_id: str):
    """Testa conectividade do proxy da conta. Retorna o IP externo detectado."""
    acc = await accounts_store.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("proxy_server"):
        return {"ok": False, "error": "Sem proxy configurado", "ip": None}
    result = await test_proxy(
        acc["proxy_server"],
        acc.get("proxy_user"),
        acc.get("proxy_pass"),
    )
    return result


@app.post("/api/accounts/{account_id}/skip-cooldown")
async def skip_cooldown(account_id: str):
    """Interrompe o cooldown de 10min entre rotas pra essa conta."""
    evt = cooldown_skip_events.get(account_id)
    if evt:
        evt.set()
        return {"ok": True, "skipped": True}
    return {"ok": False, "reason": "account not running or not in cooldown"}


@app.post("/api/accounts/{account_id}/toggle")
async def toggle_account(account_id: str):
    acc = await accounts_store.get_account(account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    new_state = not acc["enabled"]
    updated = await accounts_store.set_enabled(account_id, new_state)
    if new_state:
        await start_worker_for_account(account_id)
    else:
        await stop_worker_for_account(account_id)
    await broadcast({"type": "account_updated", "account": updated})
    return updated


# ===== Endpoints: Routes =====
@app.get("/api/routes")
async def list_routes():
    routes = await routes_store.list_routes()
    results = await routes_store.list_results()
    for r in routes:
        r["has_result"] = r["id"] in results
        res = results.get(r["id"])
        r["min_price_k"] = get_min_price(res) if res else None
    return {
        "routes": routes,
        "queue": queue_items,
    }


@app.post("/api/routes")
async def add_route(route: RouteIn):
    new_route = await routes_store.add_route(route.dict())
    await broadcast({"type": "route_added", "route": new_route})
    return new_route


@app.delete("/api/routes/{route_id}")
async def remove_route(route_id: str):
    ok = await routes_store.remove_route(route_id)
    if not ok:
        raise HTTPException(404, "Route not found")
    async with queue_cond:
        if route_id in queue_items:
            queue_items.remove(route_id)
    await broadcast({"type": "route_removed", "route_id": route_id})
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True}


@app.post("/api/routes/{route_id}/enqueue")
async def enqueue_route(route_id: str):
    route = await routes_store.get_route(route_id)
    if not route:
        raise HTTPException(404, "Route not found")
    async with queue_cond:
        if route_id in queue_items:
            return {"ok": True, "already_queued": True}
        await routes_store.update_status(route_id, "pending")
        queue_items.append(route_id)
        queue_cond.notify()
    await broadcast({"type": "route_enqueued", "route_id": route_id})
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True}


@app.post("/api/routes/enqueue-all")
async def enqueue_all():
    """Enfileira somente rotas NAO conclu\u00eddas (pending, error, blocked).
    Rotas com status 'completed' sao puladas - elas ja tem resultado.
    Rotas com is_partial=True (bloqueadas no meio) sao enfileiradas para retomar."""
    routes = await routes_store.list_routes()
    count = 0
    skipped_completed = 0
    async with queue_cond:
        for r in routes:
            if r["id"] in queue_items or r["status"] == "searching":
                continue
            # Pula rotas ja concluidas (resultado completo)
            if r["status"] == "completed" and not r.get("is_partial"):
                skipped_completed += 1
                continue
            await routes_store.update_status(r["id"], "pending")
            queue_items.append(r["id"])
            count += 1
        if count > 0:
            queue_cond.notify_all()
    await broadcast({"type": "batch_enqueued", "count": count})
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True, "enqueued": count, "skipped_completed": skipped_completed}


@app.post("/api/routes/enqueue-all-force")
async def enqueue_all_force():
    """Refaz TODAS as rotas do zero (apaga resultados existentes).
    Use com cuidado: rotas ja conclu\u00eddas perdem seus resultados."""
    routes = await routes_store.list_routes()
    count = 0
    async with queue_cond:
        for r in routes:
            if r["id"] in queue_items or r["status"] == "searching":
                continue
            # Reset limpa is_partial E apaga resultado existente
            await routes_store.reset_status(r["id"])
            queue_items.append(r["id"])
            count += 1
        if count > 0:
            queue_cond.notify_all()
    await broadcast({"type": "batch_enqueued", "count": count, "force": True})
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True, "enqueued": count}


@app.post("/api/routes/{route_id}/move-up")
async def move_route_up(route_id: str):
    """Move a rota 1 posicao pra cima na fila."""
    async with queue_cond:
        if route_id not in queue_items:
            raise HTTPException(400, "Rota nao esta na fila")
        idx = queue_items.index(route_id)
        if idx == 0:
            return {"ok": True, "position": 0}  # ja esta no topo
        queue_items[idx - 1], queue_items[idx] = queue_items[idx], queue_items[idx - 1]
        new_position = idx - 1
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True, "position": new_position}


@app.post("/api/routes/{route_id}/move-down")
async def move_route_down(route_id: str):
    """Move a rota 1 posicao pra baixo na fila."""
    async with queue_cond:
        if route_id not in queue_items:
            raise HTTPException(400, "Rota nao esta na fila")
        idx = queue_items.index(route_id)
        if idx == len(queue_items) - 1:
            return {"ok": True, "position": idx}  # ja esta no final
        queue_items[idx], queue_items[idx + 1] = queue_items[idx + 1], queue_items[idx]
        new_position = idx + 1
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True, "position": new_position}


@app.post("/api/routes/{route_id}/move-to-top")
async def move_route_to_top(route_id: str):
    """Move a rota pro topo da fila (proxima a ser processada)."""
    async with queue_cond:
        if route_id not in queue_items:
            raise HTTPException(400, "Rota nao esta na fila")
        queue_items.remove(route_id)
        queue_items.insert(0, route_id)
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True, "position": 0}


@app.post("/api/routes/{route_id}/retry")
async def retry_route(route_id: str):
    route = await routes_store.get_route(route_id)
    if not route:
        raise HTTPException(404, "Route not found")
    await routes_store.reset_status(route_id)
    async with queue_cond:
        if route_id not in queue_items:
            queue_items.append(route_id)
            queue_cond.notify()
    await broadcast({"type": "route_enqueued", "route_id": route_id})
    await broadcast({"type": "queue_updated", "queue": list(queue_items)})
    return {"ok": True}


# ===== Endpoints: Results + WhatsApp =====
@app.get("/api/results/{route_id}")
async def get_result(route_id: str):
    result = await routes_store.get_result(route_id)
    if not result:
        raise HTTPException(404, "Result not found")
    return result


@app.post("/api/routes/{route_id}/whatsapp-text")
async def preview_whatsapp_text(route_id: str, req: WhatsAppRequest = WhatsAppRequest()):
    """Retorna o texto formatado que SERIA enviado pro WhatsApp (sem enviar).
    Usado pelo botao 'Copiar texto' no frontend."""
    route = await routes_store.get_route(route_id)
    if not route:
        raise HTTPException(404, "Route not found")
    result = await routes_store.get_result(route_id)
    if not result:
        raise HTTPException(400, "Sem resultado pra formatar")
    text = format_result_text(result, max_price_filter=req.max_price_k)
    return {"text": text}


@app.post("/api/routes/{route_id}/send-whatsapp")
async def send_whatsapp_for_route(route_id: str, req: WhatsAppRequest = WhatsAppRequest()):
    route = await routes_store.get_route(route_id)
    if not route:
        raise HTTPException(404, "Route not found")
    result = await routes_store.get_result(route_id)
    if not result:
        raise HTTPException(400, "Sem resultado para enviar")

    config = load_config()
    evo = config.get("evolution_api")
    if not evo or not evo.get("api_key"):
        raise HTTPException(500, "Evolution API nao configurada em config.json")

    text = format_result_text(result, max_price_filter=req.max_price_k)
    sent = await send_whatsapp(text, evo)
    if sent:
        await routes_store.mark_whatsapp_sent(route_id)
        await broadcast({"type": "whatsapp_sent", "route_id": route_id})
        return {"ok": True, "sent": True}
    return {"ok": False, "sent": False}


# ===== WebSocket =====
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    active_connections.append(ws)
    routes = await routes_store.list_routes()
    results = await routes_store.list_results()
    for r in routes:
        r["has_result"] = r["id"] in results
        res = results.get(r["id"])
        r["min_price_k"] = get_min_price(res) if res else None
    accounts = await accounts_store.list_accounts()
    await ws.send_json({
        "type": "state",
        "routes": routes,
        "accounts": accounts,
        "queue": queue_items,
        "paused": system_state.is_paused(),
    })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in active_connections:
            active_connections.remove(ws)


# ===== Serve frontend =====
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
