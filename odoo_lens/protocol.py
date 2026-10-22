from __future__ import annotations

import json
import logging
import queue
import threading
from concurrent.futures import Future
from typing import Any, BinaryIO, Callable

log = logging.getLogger("odoo_lens.protocol")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002
REQUEST_FAILED = -32803
SERVER_CANCELLED = -32802
CONTENT_MODIFIED = -32801
REQUEST_CANCELLED = -32800


class ResponseError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Cancelled(ResponseError):
    def __init__(self) -> None:
        super().__init__(REQUEST_CANCELLED, "Request cancelled")


class CancelToken:
    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled()


NEVER_CANCELLED = CancelToken()


class Transport:
    def __init__(self, rfile: BinaryIO, wfile: BinaryIO):
        self._r = rfile
        self._w = wfile
        self._wlock = threading.Lock()

    # None on EOF; a malformed body raises ValueError after being fully consumed (stream stays in sync)
    def read(self) -> Any:
        length = -1
        while True:
            line = self._r.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                if length < 0:
                    continue
                break
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        body = self._r.read(length) if length else b""
        if len(body) < length:
            return None
        return json.loads(body)

    def write(self, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), check_circular=False).encode("utf-8")
        data = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        with self._wlock:
            self._w.write(data)
            self._w.flush()


_EOF = object()

RequestHandler = Callable[[Any, CancelToken], Any]
NotificationHandler = Callable[[Any], None]


# Reader thread handles $/cancelRequest, shutdown, exit and client responses; the rest is queued.
# Dispatcher (serve_forever's thread) runs handlers in arrival order: no races, no locks needed.
class Endpoint:
    def __init__(self, transport: Transport):
        self.transport = transport
        self._requests: dict[str, RequestHandler] = {}
        self._notifications: dict[str, NotificationHandler] = {}
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._inflight: dict[Any, CancelToken] = {}
        self._client_futures: dict[str, Future] = {}
        self._next_id = 0
        self._timers: dict[Any, threading.Timer] = {}
        self._stopped = False
        self.shutdown_received = False
        self.dispatcher_thread: threading.Thread | None = None
        # "exit" hook, run on the reader thread; None means stop(); serve_stdio installs a hard exit
        self.on_exit: Callable[[int], None] | None = None

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._requests[method] = handler

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notifications[method] = handler

    def notify(self, method: str, params: Any = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.transport.write(msg)

    def request(self, method: str, params: Any = None) -> Future:
        fut: Future = Future()
        with self._lock:
            self._next_id += 1
            rid = f"odoo-lens-{self._next_id}"
            self._client_futures[rid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.transport.write(msg)
        return fut

    # thread-safe: how background threads hand work to the dispatcher (post_later: debounced per key)
    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        self._queue.put(("task", fn, args))

    def post_later(self, delay: float, key: Any, fn: Callable[..., Any], *args: Any) -> None:
        def fire() -> None:
            with self._lock:
                if self._timers.get(key) is timer:
                    del self._timers[key]
            self.post(fn, *args)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        with self._lock:
            old = self._timers.get(key)
            if old is not None:
                old.cancel()
            self._timers[key] = timer
        timer.start()

    def stop(self) -> None:
        self._stopped = True
        self._queue.put(_EOF)

    def serve_forever(self) -> None:
        self.dispatcher_thread = threading.current_thread()
        reader = threading.Thread(target=self._reader_loop, name="lsp-reader", daemon=True)
        reader.start()
        while not self._stopped:
            item = self._queue.get()
            if item is _EOF:
                break
            kind = item[0]
            try:
                if kind == "req":
                    self._handle_request(item[1], item[2])
                elif kind == "notif":
                    self._handle_notification(item[1])
                else:
                    item[1](*item[2])
            except Exception:
                log.exception("unhandled error while processing %s", kind)
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    def _reader_loop(self) -> None:
        while True:
            try:
                msg = self.transport.read()
            except ValueError as exc:
                log.warning("dropping malformed message: %s", exc)
                self.transport.write(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": str(exc)}}
                )
                continue
            except Exception:
                log.exception("transport read failed")
                break
            if msg is None:
                break
            for one in msg if isinstance(msg, list) else (msg,):
                if isinstance(one, dict):
                    self._route(one)
        self._queue.put(_EOF)

    def _route(self, msg: dict) -> None:
        method = msg.get("method")
        if method is not None:
            # never queued behind a long request: Claude Code may not await shutdown (orphaned server)
            if method == "shutdown" and "id" in msg:
                self.shutdown_received = True
                self.transport.write({"jsonrpc": "2.0", "id": msg["id"], "result": None})
                return
            if method == "exit":
                code = 0 if self.shutdown_received else 1
                if self.on_exit is not None:
                    self.on_exit(code)
                else:
                    self.stop()
                return
            if "id" in msg:
                token = CancelToken()
                with self._lock:
                    self._inflight[msg["id"]] = token
                self._queue.put(("req", msg, token))
            elif method == "$/cancelRequest":
                rid = (msg.get("params") or {}).get("id")
                with self._lock:
                    token = self._inflight.get(rid)
                if token is not None:
                    token.cancelled = True
            else:
                self._queue.put(("notif", msg))
            return
        rid = msg.get("id")
        with self._lock:
            fut = self._client_futures.pop(rid, None) if isinstance(rid, str) else None
        if fut is None:
            return
        err = msg.get("error")
        if err:
            fut.set_exception(ResponseError(err.get("code", INTERNAL_ERROR), err.get("message", ""), err.get("data")))
        else:
            fut.set_result(msg.get("result"))

    def _handle_request(self, msg: dict, token: CancelToken) -> None:
        rid = msg["id"]
        method = msg["method"]
        try:
            if token.cancelled:
                raise Cancelled()
            handler = self._requests.get(method)
            if handler is None:
                raise ResponseError(METHOD_NOT_FOUND, f"Unhandled method {method}")
            result = handler(msg.get("params"), token)
            resp: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except ResponseError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            resp = {"jsonrpc": "2.0", "id": rid, "error": error}
        except Exception as exc:
            log.exception("request %s failed", method)
            resp = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"},
            }
        finally:
            with self._lock:
                self._inflight.pop(rid, None)
        self.transport.write(resp)

    def _handle_notification(self, msg: dict) -> None:
        handler = self._notifications.get(msg["method"])
        if handler is not None:
            handler(msg.get("params"))
