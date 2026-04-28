"""
Persistencia de rotas cadastradas + resultados em disco (JSON).
Thread-safe via asyncio.Lock.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime


# Use /app/data if it exists (Docker volume), else local dir (dev local).
# Allows JSON state to survive Docker container rebuilds.
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(__file__)
ROUTES_FILE = os.path.join(_DATA_DIR, "routes.json")
RESULTS_FILE = os.path.join(_DATA_DIR, "results.json")


class RoutesStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.routes = []
        self.results = {}  # route_id -> result dict
        self._load()

    def _load(self):
        if os.path.exists(ROUTES_FILE):
            try:
                with open(ROUTES_FILE) as f:
                    self.routes = json.load(f)
            except Exception:
                self.routes = []
        if os.path.exists(RESULTS_FILE):
            try:
                with open(RESULTS_FILE) as f:
                    self.results = json.load(f)
            except Exception:
                self.results = {}
        # Garante campos de retry em rotas antigas
        for r in self.routes:
            r.setdefault("empty_retry_count", 0)
            r.setdefault("retry_not_before", None)

    async def _save(self):
        with open(ROUTES_FILE, "w") as f:
            json.dump(self.routes, f, indent=2, ensure_ascii=False)
        with open(RESULTS_FILE, "w") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)

    async def list_routes(self):
        async with self._lock:
            return list(self.routes)

    async def list_results(self):
        async with self._lock:
            return dict(self.results)

    async def get_route(self, route_id):
        async with self._lock:
            for r in self.routes:
                if r["id"] == route_id:
                    return dict(r)
            return None

    async def get_result(self, route_id):
        async with self._lock:
            return self.results.get(route_id)

    async def add_route(self, data):
        async with self._lock:
            route = {
                "id": str(uuid.uuid4())[:8],
                "origin": data["origin"].upper().strip(),
                "dest": data["dest"].upper().strip(),
                "program": data["program"].upper().strip(),
                "cabin": data.get("cabin", "economy"),
                "direction": data.get("direction", "roundtrip"),
                "months": data.get("months"),
                "status": "pending",  # pending, searching, completed, error, blocked
                "created_at": datetime.now().isoformat(),
                "last_searched_at": None,
                "last_error": None,
                "whatsapp_sent_at": None,
                # Auto-retry: quando resultado vem vazio
                "empty_retry_count": 0,
                "retry_not_before": None,  # epoch timestamp ou None
            }
            self.routes.append(route)
            await self._save()
            return route

    async def mark_for_empty_retry(self, route_id, retry_delay_seconds=1800):
        """Marca uma rota pra re-tentar depois de X segundos (default 30min).
        Incrementa empty_retry_count e seta retry_not_before."""
        async with self._lock:
            for r in self.routes:
                if r["id"] == route_id:
                    r["empty_retry_count"] = (r.get("empty_retry_count") or 0) + 1
                    r["retry_not_before"] = datetime.now().timestamp() + retry_delay_seconds
                    r["status"] = "pending"
                    r["is_partial"] = False
                    await self._save()
                    return dict(r)
            return None

    async def clear_retry_state(self, route_id):
        """Limpa estado de retry (quando resultado bem-sucedido salvo)."""
        async with self._lock:
            for r in self.routes:
                if r["id"] == route_id:
                    r["empty_retry_count"] = 0
                    r["retry_not_before"] = None
                    await self._save()
                    return
            return

    async def remove_route(self, route_id):
        async with self._lock:
            before = len(self.routes)
            self.routes = [r for r in self.routes if r["id"] != route_id]
            if route_id in self.results:
                del self.results[route_id]
            await self._save()
            return len(self.routes) < before

    async def update_status(self, route_id, status, error=None):
        async with self._lock:
            for r in self.routes:
                if r["id"] == route_id:
                    r["status"] = status
                    if status == "completed":
                        r["last_searched_at"] = datetime.now().isoformat()
                        r["last_error"] = None
                    elif status == "error":
                        r["last_error"] = error
                    elif status in ("pending", "searching"):
                        # Quando re-enfileira ou comeca a buscar, limpa erro antigo
                        # pra nao confundir o usuario com mensagens obsoletas
                        r["last_error"] = None
                    await self._save()
                    return dict(r)
            return None

    async def save_result(self, route_id, result):
        async with self._lock:
            self.results[route_id] = result
            for r in self.routes:
                if r["id"] == route_id:
                    r["status"] = "completed"
                    r["last_searched_at"] = datetime.now().isoformat()
                    r["last_error"] = None
                    r["is_partial"] = False
                    break
            await self._save()

    async def save_partial_result(self, route_id, result):
        """Save partial result (not completed - will be resumed)."""
        async with self._lock:
            self.results[route_id] = result
            for r in self.routes:
                if r["id"] == route_id:
                    r["is_partial"] = True
                    break
            await self._save()

    async def mark_whatsapp_sent(self, route_id):
        async with self._lock:
            for r in self.routes:
                if r["id"] == route_id:
                    r["whatsapp_sent_at"] = datetime.now().isoformat()
                    await self._save()
                    return True
            return False

    async def reset_status(self, route_id):
        """Reset route to pending (for re-running from scratch)."""
        async with self._lock:
            for r in self.routes:
                if r["id"] == route_id:
                    r["status"] = "pending"
                    r["last_error"] = None
                    r["is_partial"] = False
                    # Clear any existing (partial) result so retry starts fresh
                    if route_id in self.results:
                        del self.results[route_id]
                    await self._save()
                    return dict(r)
            return None
