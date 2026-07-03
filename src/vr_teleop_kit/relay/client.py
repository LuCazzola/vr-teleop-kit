"""Background WebSocket client for a WebXR VR relay.

`RelayClientXR` owns the connection lifecycle (reconnect with backoff, TLS
setup) and buffers the most recent 'xr_frame' message under a lock, so the
caller can poll `latest_frame()` from a different thread (e.g. a ROS timer
callback) without touching asyncio/websockets directly.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import time
from typing import Any, Callable

try:
    import websockets
    WEBSOCKETS_IMPORT_ERROR: Exception | None = None
except ImportError as exc:
    websockets = None
    WEBSOCKETS_IMPORT_ERROR = exc


class RelayClientXR:
    """Connects to a VR relay over WebSocket and tracks the latest xr_frame."""

    def __init__(
        self,
        url_provider: Callable[[], str],
        *,
        reconnect_backoff_s: float = 1.0,
        max_reconnect_backoff_s: float = 5.0,
        backoff_multiplier: float = 1.5,
        info: Callable[[str], None] | None = None,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        if websockets is None:
            raise RuntimeError(
                "RelayClientXR requires the 'websockets' Python package. "
                f"Import failed with: {WEBSOCKETS_IMPORT_ERROR}"
            )

        self._url_provider = url_provider
        self._reconnect_backoff_s = float(reconnect_backoff_s)
        self._max_reconnect_backoff_s = float(max_reconnect_backoff_s)
        self._backoff_multiplier = float(backoff_multiplier)
        self._info = info
        self._warn = warn

        self._lock = threading.Lock()
        self._latest_frame: dict[str, Any] | None = None
        self._last_frame_time = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Set once the background asyncio loop is running / a socket is open,
        # so `send()` can schedule outbound messages from the ROS thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None

    def start(self) -> None:
        """Start the background reconnect-and-listen thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop; does not block."""
        self._stop.set()

    def latest_frame(self) -> tuple[dict[str, Any] | None, float]:
        """Return (latest xr_frame message or None, time.time() it was received)."""
        with self._lock:
            return self._latest_frame, self._last_frame_time

    def send(self, message: dict[str, Any]) -> None:
        """Fire-and-forget a JSON message to the relay from any thread.

        The relay broadcasts it to the other connected clients (e.g. the Quest
        browser). If the socket is down the message is silently dropped, so
        callers can publish every tick without guarding on connection state.
        """
        loop = self._loop
        if loop is None:
            return
        try:
            text = json.dumps(message)
        except (TypeError, ValueError):
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_text(text), loop)
        except RuntimeError:
            # Loop stopped between the None-check and scheduling — drop.
            pass

    async def _send_text(self, text: str) -> None:
        """Send one already-serialized message on the current socket, if any."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(text)
        except Exception:
            pass

    def _run(self) -> None:
        asyncio.run(self._runner())

    @staticmethod
    def _ssl_context_for(url: str) -> ssl.SSLContext | None:
        """Create TLS settings for secure relay connections.
        Localhost certificates are accepted to support development relays.
        """
        if not url.startswith("wss://"):
            return None
        ctx = ssl.create_default_context()
        if "://localhost" in url or "://127.0.0.1" in url:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _runner(self) -> None:
        """Reconnect-with-backoff loop that buffers incoming xr_frame messages."""
        self._loop = asyncio.get_running_loop()
        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            url = self._url_provider()
            try:
                async with websockets.connect(url, ssl=self._ssl_context_for(url)) as ws:
                    self._log_info(f"Connected to VR relay at {url}")
                    self._ws = ws
                    backoff = self._reconnect_backoff_s
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") == "xr_frame":
                            with self._lock:
                                self._latest_frame = msg
                                self._last_frame_time = time.time()
            except Exception as exc:
                if not self._stop.is_set():
                    self._log_warn(f"VR relay connection failed ({exc}). Retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(self._max_reconnect_backoff_s, backoff * self._backoff_multiplier)
            finally:
                # Drop the socket handle so `send()` no-ops until we reconnect.
                self._ws = None

    def _log_info(self, message: str) -> None:
        if self._info is not None:
            self._info(message)

    def _log_warn(self, message: str) -> None:
        if self._warn is not None:
            self._warn(message)
