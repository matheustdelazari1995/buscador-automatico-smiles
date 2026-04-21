"""
Gerencia estado global do sistema em system_state.json.
Por enquanto so tem a flag `paused`, mas da pra crescer.
"""

import asyncio
import json
import os


STATE_FILE = os.path.join(os.path.dirname(__file__), "system_state.json")


class SystemState:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.paused = False
        self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                    self.paused = bool(data.get("paused", False))
            except Exception:
                self.paused = False

    async def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({"paused": self.paused}, f, indent=2)

    async def set_paused(self, value: bool):
        async with self._lock:
            self.paused = value
            await self._save()
            return self.paused

    def is_paused(self) -> bool:
        return self.paused
