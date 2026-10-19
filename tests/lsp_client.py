"""Minimal synchronous LSP client used by the end-to-end tests and benchmarks.

Spawns ``bin/odoo-lens`` as a subprocess and talks JSON-RPC over its stdio, the
same way Claude Code does.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "bin", "odoo-lens")

sys.path.insert(0, ROOT)
from odoo_lens.documents import path_to_uri  # noqa: E402


class LspClient:
    def __init__(self, cwd: str | None = None, env: dict | None = None, stderr_path: str | None = None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        self._stderr = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [LAUNCHER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            cwd=cwd or ROOT,
            env=full_env,
        )
        self._next = 0
        self._responses: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self.notifications: list[dict] = []
        self._notif_cond = threading.Condition()
        self.server_requests: list[dict] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- transport ---------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def _read_loop(self) -> None:
        out = self.proc.stdout
        assert out is not None
        while True:
            length = None
            while True:
                line = out.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    break
                k, _, v = line.partition(b":")
                if k.lower() == b"content-length":
                    length = int(v)
            if length is None:
                continue
            msg = json.loads(out.read(length))
            if "id" in msg and "method" not in msg:
                with self._lock:
                    q = self._responses.get(msg["id"])
                if q is not None:
                    q.put(msg)
            elif "id" in msg:  # server -> client request: answer with null
                self.server_requests.append(msg)
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            else:
                with self._notif_cond:
                    self.notifications.append(msg)
                    self._notif_cond.notify_all()

    # -- API -----------------------------------------------------------------------

    def request(self, method: str, params: Any = None, timeout: float = 120.0) -> Any:
        with self._lock:
            self._next += 1
            rid = self._next
            q: queue.Queue = queue.Queue()
            self._responses[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        finally:
            with self._lock:
                self._responses.pop(rid, None)
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {msg['error']}")
        return msg.get("result")

    def notify(self, method: str, params: Any = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, root: str, capabilities: dict | None = None, options: dict | None = None) -> dict:
        caps = capabilities or {
            "general": {"positionEncodings": ["utf-16"]},
            "textDocument": {
                "synchronization": {"didSave": True, "dynamicRegistration": False},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "publishDiagnostics": {"relatedInformation": True, "versionSupport": True},
                "definition": {"linkSupport": False},
                "references": {},
                "implementation": {},
                "callHierarchy": {},
            },
            "workspace": {"workspaceFolders": True, "symbol": {}},
        }
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": path_to_uri(root),
                "rootPath": root,
                "workspaceFolders": [{"uri": path_to_uri(root), "name": os.path.basename(root)}],
                "capabilities": caps,
                "initializationOptions": options or {},
            },
        )
        self.notify("initialized", {})
        return result

    def open(self, path: str, text: str | None = None, language_id: str | None = None) -> None:
        if text is None:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        lang = language_id or {".py": "python", ".xml": "xml", ".csv": "csv", ".js": "javascript"}.get(os.path.splitext(path)[1], "plaintext")
        self.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(path), "languageId": lang, "version": 1, "text": text}})

    def change(self, path: str, text: str, version: int) -> None:
        self.notify("textDocument/didChange", {"textDocument": {"uri": path_to_uri(path), "version": version}, "contentChanges": [{"text": text}]})

    def close(self, path: str) -> None:
        self.notify("textDocument/didClose", {"textDocument": {"uri": path_to_uri(path)}})

    def at(self, method: str, path: str, line: int, character: int, **extra: Any) -> Any:
        params = {"textDocument": {"uri": path_to_uri(path)}, "position": {"line": line, "character": character}}
        params.update(extra)
        return self.request(method, params)

    def wait_diagnostics(self, path: str, timeout: float = 30.0) -> list[dict] | None:
        uri = path_to_uri(path)
        deadline = time.monotonic() + timeout
        with self._notif_cond:
            while True:
                for msg in reversed(self.notifications):
                    if msg.get("method") == "textDocument/publishDiagnostics" and msg["params"]["uri"] == uri:
                        return msg["params"]["diagnostics"]
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._notif_cond.wait(left)

    def shutdown(self) -> int:
        try:
            self.request("shutdown", None, timeout=10)
            self.notify("exit")
        except Exception:
            pass
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return -9
        finally:
            if self._stderr not in (None, subprocess.DEVNULL):
                self._stderr.close()  # type: ignore[union-attr]
