"""
Persistencia de rotas cadastradas + resultados em disco (JSON).
Thread-safe via asyncio.Lock.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime


ROUTES_FILE = os.path.join(os.path.dirname(__file__), "routes.json")
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "results.json")


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
            }
            self.routes.append(route)
            await self._save()
            return route

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
