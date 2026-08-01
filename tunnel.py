from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

QUICK_TUNNEL_PATTERN = re.compile(
    r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE
)


class QuickTunnel:
    """Manage an optional Cloudflare Quick Tunnel subprocess."""

    def __init__(self, local_url: str, search_paths: list[Path] | None = None) -> None:
        self.local_url = str(local_url or "").rstrip("/")
        self.search_paths = [Path(path) for path in search_paths or []]
        self.url = ""
        self.error = ""
        self.ready = False
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.returncode is None and self.url)

    def binary_path(self) -> Path | None:
        """Find cloudflared without downloading or modifying the host."""
        command = shutil.which("cloudflared")
        if command:
            return Path(command)
        names = (
            ("cloudflared.exe", "cloudflared") if os.name == "nt" else ("cloudflared",)
        )
        for root in self.search_paths:
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    return candidate
        return None

    def status(self) -> dict[str, object]:
        """Return a dashboard-safe tunnel status."""
        return {
            "installed": self.binary_path() is not None,
            "running": self.running,
            "ready": self.running and self.ready,
            "url": self.url if self.running else "",
            "error": self.error,
        }

    async def start(self, timeout: float = 40.0) -> str:
        """Start cloudflared and wait for its temporary HTTPS URL."""
        async with self._start_lock:
            if self.running:
                return self.url
            binary = self.binary_path()
            if binary is None:
                raise RuntimeError(
                    "未找到 cloudflared，请先安装 Cloudflare Tunnel 客户端"
                )
            await self.stop()
            self.url = ""
            self.error = ""
            self.ready = False
            try:
                self._process = await asyncio.create_subprocess_exec(
                    str(binary),
                    "tunnel",
                    "--no-autoupdate",
                    "--url",
                    self.local_url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as exc:
                raise RuntimeError(f"无法启动 cloudflared：{exc}") from exc
            self._reader_task = asyncio.create_task(self._read_output())
            deadline = asyncio.get_running_loop().time() + max(2.0, timeout)
            while not self.url and asyncio.get_running_loop().time() < deadline:
                if self._process.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if not self.url:
                message = self.error or "cloudflared 未返回临时公网地址"
                await self.stop()
                raise RuntimeError(message)
            self.ready = True
            return self.url

    async def stop(self) -> None:
        """Stop the subprocess and clear the temporary public URL."""
        process = self._process
        reader = self._reader_task
        self._process = None
        self._reader_task = None
        self.url = ""
        self.ready = False
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self.error = "无法读取 cloudflared 输出"
            return
        lines: list[str] = []
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)
                    del lines[:-8]
                match = QUICK_TUNNEL_PATTERN.search(line)
                if match and not self.url:
                    self.url = match.group(0).rstrip("/")
            if not self.url and lines:
                self.error = lines[-1][-300:]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"读取 cloudflared 状态失败：{exc}"
