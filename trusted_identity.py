from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger


@dataclass(frozen=True, slots=True)
class TrustedIdentity:
    qq: str
    display_name: str
    expires_at: float


class TrustedIdentityStore:
    """Persist trusted browser credentials without storing their raw tokens."""

    VERSION = 1
    MAX_DEVICES_PER_QQ = 8
    TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")

    def __init__(self, path: Path, *, ttl_days: int = 30) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(1, min(int(ttl_days), 365)) * 86400
        self._lock = asyncio.Lock()
        self._records = self._load()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def issue(
        self, qq: str, display_name: str = "", *, now: float | None = None
    ) -> tuple[str, TrustedIdentity]:
        normalized_qq = str(qq or "").strip()
        if not normalized_qq.isdigit():
            raise ValueError("受信任浏览器绑定的 QQ 号无效")
        current = time.time() if now is None else float(now)
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        record = {
            "qq": normalized_qq,
            "display_name": str(display_name or "").strip()[:40],
            "created_at": current,
            "last_used_at": current,
            "expires_at": current + self.ttl_seconds,
        }
        async with self._lock:
            self._purge(current)
            self._records[digest] = record
            same_qq = sorted(
                (
                    (key, value)
                    for key, value in self._records.items()
                    if value["qq"] == normalized_qq
                ),
                key=lambda item: float(item[1].get("last_used_at") or 0),
                reverse=True,
            )
            for key, _value in same_qq[self.MAX_DEVICES_PER_QQ :]:
                self._records.pop(key, None)
            self._save()
        return token, self._identity(record)

    async def resolve(
        self, token: str, *, now: float | None = None
    ) -> TrustedIdentity | None:
        normalized = str(token or "").strip()
        if not self.TOKEN_PATTERN.fullmatch(normalized):
            return None
        current = time.time() if now is None else float(now)
        async with self._lock:
            changed = self._purge(current)
            record = self._records.get(self._digest(normalized))
            if record is None:
                if changed:
                    self._save()
                return None
            if current - float(record.get("last_used_at") or 0) >= 86400:
                record["last_used_at"] = current
                changed = True
            if changed:
                self._save()
            return self._identity(record)

    async def revoke_token(self, token: str) -> bool:
        normalized = str(token or "").strip()
        if not self.TOKEN_PATTERN.fullmatch(normalized):
            return False
        async with self._lock:
            removed = self._records.pop(self._digest(normalized), None) is not None
            if removed:
                self._save()
            return removed

    async def revoke_qq(self, qq: str) -> int:
        normalized_qq = str(qq or "").strip()
        async with self._lock:
            keys = [
                key
                for key, value in self._records.items()
                if value.get("qq") == normalized_qq
            ]
            for key in keys:
                self._records.pop(key, None)
            if keys:
                self._save()
            return len(keys)

    @property
    def count(self) -> int:
        return len(self._records)

    @staticmethod
    def _identity(record: dict[str, Any]) -> TrustedIdentity:
        return TrustedIdentity(
            qq=str(record.get("qq") or ""),
            display_name=str(record.get("display_name") or ""),
            expires_at=float(record.get("expires_at") or 0),
        )

    def _purge(self, now: float) -> bool:
        expired = [
            key
            for key, value in self._records.items()
            if float(value.get("expires_at") or 0) <= now
        ]
        for key in expired:
            self._records.pop(key, None)
        return bool(expired)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            devices = payload.get("devices", {}) if isinstance(payload, dict) else {}
            if not isinstance(devices, dict):
                raise ValueError("devices 不是对象")
            records: dict[str, dict[str, Any]] = {}
            for digest, raw in devices.items():
                if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                    continue
                if not isinstance(raw, dict) or not str(raw.get("qq") or "").isdigit():
                    continue
                if float(raw.get("expires_at") or 0) <= time.time():
                    continue
                records[str(digest)] = {
                    "qq": str(raw.get("qq")),
                    "display_name": str(raw.get("display_name") or "")[:40],
                    "created_at": float(raw.get("created_at") or 0),
                    "last_used_at": float(raw.get("last_used_at") or 0),
                    "expires_at": float(raw.get("expires_at") or 0),
                }
            return records
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[GameCompanion] 读取受信任浏览器绑定失败: %s", exc)
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{secrets.token_hex(4)}.tmp"
        )
        payload = {
            "version": self.VERSION,
            "devices": self._records,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
