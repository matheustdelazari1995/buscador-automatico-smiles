"""
Gerencia contas AwardTool em accounts.json.
Cada conta tem seu proprio perfil Chrome (profile_dir).
"""

import asyncio
import json
import os
import uuid
from datetime import datetime


ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts.json")


class AccountsStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.accounts = []
        self._load()

    def _load(self):
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE) as f:
                    self.accounts = json.load(f)
            except Exception:
                self.accounts = []
        # Ensure runtime fields exist on every loaded account
        for a in self.accounts:
            a.setdefault("status", "idle")
            a.setdefault("current_route_id", None)
            a.setdefault("blocked_until", None)
            a.setdefault("last_error", None)

    async def _save(self):
        with open(ACCOUNTS_FILE, "w") as f:
            json.dump(self.accounts, f, indent=2, ensure_ascii=False)

    async def list_accounts(self):
        async with self._lock:
            return [dict(a) for a in self.accounts]

    async def get_account(self, account_id):
        async with self._lock:
            for a in self.accounts:
                if a["id"] == account_id:
                    return dict(a)
            return None

    async def add_account(self, data):
        async with self._lock:
            aid = data.get("id") or f"conta{len(self.accounts) + 1}"
            account = {
                "id": aid,
                "name": data.get("name") or aid,
                "profile_dir": data.get("profile_dir") or f".browser-profile-{aid}",
                "enabled": data.get("enabled", True),
                "notes": data.get("notes", ""),
                "created_at": datetime.now().isoformat(),
                # Runtime fields
                "status": "idle",  # idle, searching, blocked, disabled
                "current_route_id": None,
                "blocked_until": None,
                "last_error": None,
            }
            self.accounts.append(account)
            await self._save()
            return account

    async def remove_account(self, account_id):
        async with self._lock:
            before = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != account_id]
            await self._save()
            return len(self.accounts) < before

    async def set_enabled(self, account_id, enabled):
        async with self._lock:
            for a in self.accounts:
                if a["id"] == account_id:
                    a["enabled"] = enabled
                    if not enabled:
                        a["status"] = "disabled"
                    elif a["status"] == "disabled":
                        a["status"] = "idle"
                    await self._save()
                    return dict(a)
            return None

    async def set_status(self, account_id, status, current_route_id=None, blocked_until=None, error=None):
        async with self._lock:
            for a in self.accounts:
                if a["id"] == account_id:
                    a["status"] = status
                    a["current_route_id"] = current_route_id
                    if blocked_until is not None:
                        a["blocked_until"] = blocked_until
                    if status != "blocked":
                        a["blocked_until"] = None
                    if error is not None:
                        a["last_error"] = error
                    elif status in ("idle", "searching"):
                        a["last_error"] = None
                    await self._save()
                    return dict(a)
            return None

    async def enabled_accounts(self):
        async with self._lock:
            return [dict(a) for a in self.accounts if a.get("enabled")]
