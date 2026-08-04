from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from astrbot.api import logger

from .models import GameRoom
from .room_manager import RoomManager


class GameRoomServer:
    """Serve the mobile game UI without sharing AstrBot's dashboard port."""

    ASSETS = {"index.html", "app.css", "app.js", "lucide.min.js"}
    TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,80}")

    def __init__(
        self,
        plugin: Any,
        manager: RoomManager,
        *,
        host: str,
        port: int,
        web_root: Path,
    ) -> None:
        self.plugin = plugin
        self.manager = manager
        self.host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        self.requested_port = max(1, min(int(port), 65535))
        self.port = self.requested_port
        self.web_root = Path(web_root)
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and self._site is not None

    @property
    def local_base_url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::", "[::]"} else self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    async def start(self) -> None:
        """Start on the configured port or one of the next ten ports."""
        if self.running:
            return
        missing = [name for name in self.ASSETS if not (self.web_root / name).is_file()]
        if missing:
            raise RuntimeError("游戏页面静态资源不完整：" + "、".join(sorted(missing)))
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        last_error: OSError | None = None
        for candidate in range(
            self.requested_port, min(65535, self.requested_port + 10) + 1
        ):
            site = web.TCPSite(self._runner, self.host, candidate)
            try:
                await site.start()
            except OSError as exc:
                last_error = exc
                continue
            self._site = site
            self.port = candidate
            return
        await self.stop()
        raise RuntimeError(
            f"无法监听游戏端口 {self.requested_port}-{min(65535, self.requested_port + 10)}：{last_error}"
        )

    def _build_app(self) -> web.Application:
        """Build the room application for the real server and isolated tests."""
        app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[self._error_middleware],
        )
        app.router.add_get("/", self._serve_index)
        app.router.add_get("/room/{access_token}", self._serve_index)
        app.router.add_get("/assets/{name}", self._serve_asset)
        app.router.add_get("/health", self._health)
        app.router.add_post("/api/room/{access_token}/join", self._join)
        app.router.add_get("/api/room/{access_token}/state", self._state)
        app.router.add_post("/api/room/{access_token}/claim", self._claim)
        app.router.add_post("/api/room/{access_token}/start", self._start_game)
        app.router.add_post("/api/room/{access_token}/move", self._move)
        app.router.add_post("/api/room/{access_token}/dice/action", self._dice_action)
        app.router.add_post(
            "/api/room/{access_token}/soup/question", self._soup_question
        )
        app.router.add_post("/api/room/{access_token}/soup/answer", self._soup_answer)
        app.router.add_post("/api/room/{access_token}/soup/hint", self._soup_hint)
        app.router.add_post("/api/room/{access_token}/soup/reverse", self._soup_reverse)
        app.router.add_post("/api/room/{access_token}/soup/correct", self._soup_correct)
        app.router.add_post(
            "/api/room/{access_token}/seat/swap/request", self._seat_swap_request
        )
        app.router.add_post(
            "/api/room/{access_token}/seat/swap/respond", self._seat_swap_respond
        )
        app.router.add_post("/api/room/{access_token}/rematch", self._rematch)
        app.router.add_post("/api/room/{access_token}/leave", self._leave)
        return app

    async def stop(self) -> None:
        """Stop only the server instance owned by this plugin."""
        site, runner = self._site, self._runner
        self._site = None
        self._runner = None
        if site is not None:
            await site.stop()
        if runner is not None:
            await runner.cleanup()

    async def _serve_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            self.web_root / "index.html",
            headers=self._headers("text/html; charset=utf-8"),
        )

    async def _serve_asset(self, request: web.Request) -> web.FileResponse:
        name = str(request.match_info.get("name") or "")
        if name not in self.ASSETS - {"index.html"}:
            raise web.HTTPNotFound()
        path = self.web_root / name
        return web.FileResponse(
            path,
            headers=self._headers(
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            ),
        )

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "plugin": "astrbot_plugin_game_companion",
                "rooms": len(self.manager.rooms),
            },
            headers=self._headers("application/json"),
        )

    async def _join(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor = await self.manager.join(room, str(payload.get("visitor_token") or ""))
        return self._response(
            {
                "visitor_token": visitor.token,
                "room": room.public_snapshot(visitor.token),
            }
        )

    async def _state(self, request: web.Request) -> web.Response:
        room = self._room(request)
        visitor_token = str(request.query.get("visitor_token") or "")
        await self.manager.heartbeat(room, visitor_token)
        return self._response({"room": room.public_snapshot(visitor_token)})

    async def _claim(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.require_visitor_identity(room, visitor_token)
        await self.manager.claim_and_start(
            room,
            visitor_token,
            str(payload.get("side") or "human_black"),
        )
        return self._response(
            {"room": room.public_snapshot(visitor_token)}
        )

    async def _start_game(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.require_visitor_identity(room, visitor_token)
        await self.manager.start_game(
            room, visitor_token, str(payload.get("side") or "human_black")
        )
        return self._response({"room": room.public_snapshot(visitor_token)})

    async def _move(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.player_move(
            room,
            visitor_token,
            row=int(payload.get("row", -1)),
            column=int(payload.get("column", -1)),
            from_row=int(payload.get("from_row", -1)),
            from_column=int(payload.get("from_column", -1)),
            to_row=int(payload.get("to_row", -1)),
            to_column=int(payload.get("to_column", -1)),
        )
        return self._response({"room": room.public_snapshot(visitor_token)})

    async def _rematch(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.request_rematch(room, visitor_token)
        return self._response({"room": room.public_snapshot(visitor_token)})

    async def _dice_action(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.player_dice_action(
            room, visitor_token, str(payload.get("action") or "")
        )
        return self._response({"room": room.public_snapshot(visitor_token)})

    async def _soup_question(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_turtle_soup_question(
            room,
            str(payload.get("text") or ""),
            source="web",
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": room.public_snapshot(visitor_token)})

    async def _soup_answer(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_turtle_soup_answer(
            room,
            str(payload.get("text") or ""),
            source="web",
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": room.public_snapshot(visitor_token)})

    async def _soup_hint(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        hint = await self.manager.request_turtle_soup_hint(
            room,
            source="web",
            visitor_token=visitor_token,
        )
        return self._response(
            {
                "hint": hint,
                "room": room.public_snapshot(visitor_token),
            }
        )

    async def _soup_reverse(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_reverse_turtle_soup_turn(
            room,
            str(payload.get("text") or ""),
            source="web",
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": room.public_snapshot(visitor_token)})

    async def _soup_correct(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.confirm_reverse_turtle_soup_guess(
            room, source="web", visitor_token=visitor_token
        )
        return self._response({"room": room.public_snapshot(visitor_token)})

    async def _seat_swap_request(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        request_id = await self.manager.request_seat_swap(
            room,
            visitor_token,
            int(payload.get("target_number") or 0),
        )
        return self._response(
            {
                "request_id": request_id,
                "room": room.public_snapshot(visitor_token),
            }
        )

    async def _seat_swap_respond(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        accepted = payload.get("accepted") is True
        await self.manager.resolve_seat_swap(
            room,
            visitor_token,
            str(payload.get("request_id") or ""),
            accepted=accepted,
        )
        return self._response(
            {"accepted": accepted, "room": room.public_snapshot(visitor_token)}
        )

    async def _leave(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        await self.manager.leave(room, str(payload.get("visitor_token") or ""))
        return self._response({"left": True})

    @web.middleware
    async def _error_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if request.path.startswith("/api/"):
                return web.json_response(
                    {"status": "error", "message": exc.text or exc.reason},
                    status=exc.status,
                    headers=self._headers("application/json"),
                )
            raise
        except PermissionError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=403,
                headers=self._headers("application/json"),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
                headers=self._headers("application/json"),
            )
        except RuntimeError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=503,
                headers=self._headers("application/json"),
            )
        except Exception as exc:
            logger.warning("[GameCompanion] 房间请求处理失败: %s", exc, exc_info=True)
            return web.json_response(
                {"status": "error", "message": "房间服务处理请求失败"},
                status=500,
                headers=self._headers("application/json"),
            )

    def _room(self, request: web.Request) -> GameRoom:
        token = str(request.match_info.get("access_token") or "")
        if not self.TOKEN_PATTERN.fullmatch(token):
            raise web.HTTPNotFound(text="房间链接无效")
        room = self.manager.by_access_token(token)
        if room is None:
            reason = self.manager.closed_reason_by_access_token(token)
            raise web.HTTPGone(text=reason or "房间已结束或链接已经失效")
        return room

    async def _payload(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="请求内容不是有效 JSON") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="请求内容格式无效")
        return payload

    def _require_origin(self, request: web.Request) -> None:
        if not self._origin_allowed(request):
            raise web.HTTPForbidden(text="房间来源校验失败")

    def _origin_allowed(self, request: Any) -> bool:
        origin = str(request.headers.get("Origin") or "").strip()
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return False
            origin_value = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            request_value = (
                f"{str(request.scheme).lower()}://{str(request.host).lower()}"
            )
        except Exception:
            return False
        if origin_value == request_value:
            return True
        public_urls = (
            str(getattr(self.plugin, "public_base_url", "") or ""),
            str(getattr(getattr(self.plugin, "quick_tunnel", None), "url", "") or ""),
        )
        for public_url in public_urls:
            try:
                public = urlsplit(public_url)
                if (
                    public.scheme
                    and public.netloc
                    and origin_value
                    == f"{public.scheme.lower()}://{public.netloc.lower()}"
                ):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _response(data: dict[str, Any]) -> web.Response:
        return web.json_response({"status": "ok", "data": data})

    @staticmethod
    def _headers(content_type: str) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        if content_type.startswith("text/html"):
            headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
        return headers
