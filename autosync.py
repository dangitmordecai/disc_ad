from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

class AutoSyncManager:
    """
    Drop-in file watcher that triggers your reload coroutine when commands/flows change.
    - Pass absolute or project-root-relative paths to commands_path / flows_path.
    - Provide reload_coro: an async function with no args that performs your actual
      (re)build + sync logic (whatever your project currently does for /reload).
    - Call `await start()` once (e.g., in Bot.setup_hook). Call `await stop()` on shutdown.
    - Polls once per second by default (poll_interval_sec). No extra deps required.
    """
    def __init__(
        self,
        commands_path: Path,
        flows_path: Path,
        reload_coro: Callable[[], Awaitable[None]],
        poll_interval_sec: float = 1.0,
    ) -> None:
        self.commands_path = Path(commands_path)
        self.flows_path = Path(flows_path)
        self.reload_coro = reload_coro
        self.poll_interval_sec = poll_interval_sec

        self._task: Optional[asyncio.Task] = None
        self._last_mtime_cmd = 0.0
        self._last_mtime_flow = 0.0
        self._stopped = asyncio.Event()

    def _mtime(self, p: Path) -> float:
        try:
            return p.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    async def _loop(self) -> None:
        # Initialize mtimes so that we don't reload immediately unless files differ.
        self._last_mtime_cmd = self._mtime(self.commands_path)
        self._last_mtime_flow = self._mtime(self.flows_path)

        while not self._stopped.is_set():
            try:
                m1 = self._mtime(self.commands_path)
                m2 = self._mtime(self.flows_path)
                if m1 != self._last_mtime_cmd or m2 != self._last_mtime_flow:
                    # Update before calling reload to avoid duplicate triggers
                    self._last_mtime_cmd, self._last_mtime_flow = m1, m2
                    await self.reload_coro()
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval_sec)
            except asyncio.TimeoutError:
                # normal tick
                continue
            except Exception:
                # swallow unexpected errors and keep the watcher alive
                # (your reload_coro should do its own logging)
                await asyncio.sleep(self.poll_interval_sec)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="autosync-watcher")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
