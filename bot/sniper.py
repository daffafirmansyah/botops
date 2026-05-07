"""Sniper mode: HTTP server that receives signatures from the userscript
and instantly fires on-chain mint transactions.

Architecture
------------

    [Browser + Tampermonkey userscript]
              │ POST /signature  { wallet, salt, signature, contract, ... }
              ▼
    [SniperServer (this module)]
              │ build_tx + sign + multi-RPC broadcast
              ▼
    [Ethereum / L2 RPC nodes]

The server is intentionally minimal — uses Python's stdlib `http.server`
to avoid pulling extra dependencies. For production (public IP), put it
behind Caddy / nginx with TLS.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional

from .logger import get_logger
from .utils import short_addr


log = get_logger("sniper")


@dataclass
class SignaturePayload:
    """Parsed payload from the userscript."""
    wallet: str            # lowercase 0x... (the minter address)
    salt: str              # 0x... bytes32 hex
    signature: str         # 0x... 65-byte signature
    contract: str          # lowercase 0x... NFT contract
    phase: str = ""        # phase name/index hint (best-effort)
    captured_at: int = 0   # unix ms
    raw: Dict = None       # original blob for debugging

    @classmethod
    def from_json(cls, data: Dict) -> "SignaturePayload":
        wallet = str(data.get("minter") or data.get("wallet") or "").lower()
        salt = str(data.get("salt") or "")
        sig = str(data.get("signature") or "")
        contract = str(data.get("contract") or "").lower()
        phase = str(data.get("phase") or "")
        captured = int(data.get("captured_at") or 0)
        if not (salt.startswith("0x") and sig.startswith("0x")):
            raise ValueError("salt and signature must be 0x-prefixed hex")
        if len(sig) < 130:  # 65 bytes = 130 hex chars + 0x = 132
            raise ValueError(f"signature too short ({len(sig)} chars)")
        return cls(
            wallet=wallet, salt=salt, signature=sig, contract=contract,
            phase=phase, captured_at=captured, raw=data,
        )


# Type for the fire callback: takes a payload, returns (ok, message).
FireCallback = Callable[[SignaturePayload], "tuple[bool, str]"]


class _SniperHandler(BaseHTTPRequestHandler):
    """HTTP request handler. Bound to a SniperServer at runtime."""

    server_version = "OpenSeaSniper/1.0"

    # Will be set by SniperServer
    fire_callback: FireCallback = None
    shared_secret: str = ""
    allowed_origin: str = "*"  # CORS

    def _send_json(self, status: int, payload: Dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Sniper-Source")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server convention
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Sniper-Source")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health", "/status"):
            self._send_json(200, {
                "status": "ready",
                "service": "opensea-mint-bot sniper",
                "endpoint": "POST /signature",
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/signature", "/fire"):
            self._send_json(404, {"error": "unknown endpoint"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:  # cap 64KB
            self._send_json(400, {"error": "invalid content-length"})
            return

        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"bad json: {exc}"})
            return

        # Optional shared-secret auth
        if self.shared_secret:
            received = str(data.get("shared_secret") or "")
            if received != self.shared_secret:
                log.warning("sniper: rejected POST with bad shared_secret from %s",
                            self.client_address[0])
                self._send_json(401, {"error": "unauthorized"})
                return

        try:
            payload = SignaturePayload.from_json(data)
        except ValueError as exc:
            self._send_json(400, {"error": f"invalid payload: {exc}"})
            return

        log.info(
            "sniper: signature received wallet=%s contract=%s phase=%s sig=%s…",
            short_addr(payload.wallet) if payload.wallet else "?",
            short_addr(payload.contract) if payload.contract else "?",
            payload.phase or "?",
            payload.signature[:14],
        )

        if self.fire_callback is None:
            self._send_json(503, {"error": "fire callback not registered"})
            return

        try:
            ok, msg = self.fire_callback(payload)
        except Exception as exc:  # noqa: BLE001 - return error to client
            log.exception("sniper: fire callback raised: %s", exc)
            self._send_json(500, {"error": f"fire failed: {exc}"})
            return

        if ok:
            self._send_json(200, {"status": "fired", "message": msg})
        else:
            self._send_json(500, {"status": "failed", "message": msg})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Route http.server logs through our logger at DEBUG level
        log.debug("http %s - %s", self.client_address[0], format % args)


class SniperServer:
    """Threaded HTTP server wrapper.

    Usage:
        srv = SniperServer(host="127.0.0.1", port=8888,
                           fire_callback=my_fire,
                           shared_secret="mySecret")
        srv.start()  # non-blocking; spawns thread
        ...
        srv.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8888,
        *,
        fire_callback: Optional[FireCallback] = None,
        shared_secret: str = "",
        allowed_origin: str = "*",
    ):
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # Configure handler class with our settings via subclass
        handler_cls = type(
            "_BoundHandler",
            (_SniperHandler,),
            {
                "fire_callback": staticmethod(fire_callback) if fire_callback else None,
                "shared_secret": shared_secret,
                "allowed_origin": allowed_origin,
            },
        )
        self._handler_cls = handler_cls

    def start(self) -> None:
        if self._thread is not None:
            return
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="sniper-http",
            daemon=True,
        )
        self._thread.start()
        log.info("sniper HTTP server listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._httpd = None
        self._thread = None
        log.info("sniper HTTP server stopped")

    def serve_until(self, predicate: Callable[[], bool], poll_seconds: float = 0.5) -> None:
        """Block until predicate() returns True. Useful for CLI integration."""
        try:
            while not predicate():
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            log.info("sniper: keyboard interrupt, stopping")
        finally:
            self.stop()
