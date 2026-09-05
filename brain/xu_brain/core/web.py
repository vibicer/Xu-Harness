"""Minimal static SPA server for the Xu webui, served from the brain process.

Runs on its own thread (stdlib http.server) so the asyncio WebSocket loop keeps
owning the loop. Enables the lightweight web frontend: one daemon, WS + HTTP.

Also serves plugin frontend modules under ``/plugins/<name>/<file>`` (the
``panel`` slot). A plugin's module is only reachable while
that plugin is enabled, and only the exact file its manifest declares — the
shell asks for what ``plugin.list`` advertised, nothing else is exposed.
"""
from __future__ import annotations

import functools
import http.server
import os
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path

MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}


class _SpaHandler(http.server.SimpleHTTPRequestHandler):
    """Serve `dist/` with a fallback to index.html for client-side routes.

    ``ui_lookup`` (when given) resolves ``/plugins/<name>/<file>`` to a real
    path, or ``None``. It is the sole authority on what plugin file is
    readable, so this handler never touches the plugins dir on its own.
    """

    ui_lookup: Callable[[str, str], Path | None] | None = None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "public, max-age=0, must-revalidate")
        super().end_headers()

    def guess_type(self, path: str | os.PathLike[str]) -> str:
        ext = Path(os.fspath(path)).suffix
        return MIME.get(ext, "application/octet-stream")

    def do_GET(self) -> None:
        rel = self.path.split("?", 1)[0].split("#", 1)[0]
        if rel.startswith("/plugins/") and self._serve_plugin(rel):
            return
        target = Path(self.directory) / rel.lstrip("/")
        if not target.is_file():
            self.path = "/"  # SPA fallback: send the app shell
        super().do_GET()

    def _serve_plugin(self, rel: str) -> bool:
        """Serve a plugin's declared frontend module. True when handled."""
        if self.ui_lookup is None:
            return False
        parts = rel.removeprefix("/plugins/").split("/")
        if len(parts) != 2 or not all(parts):
            self.send_error(404)
            return True
        name, filename = parts
        # A disabled plugin, or a file it never declared, resolves to None —
        # so "enabled" is enforced on every request, not just at page load.
        path = self.ui_lookup(name, filename)
        if path is None:
            self.send_error(404)
            return True
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return True
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve_web(
    dist_dir: Path,
    port: int,
    ui_lookup: Callable[[str, str], Path | None] | None = None,
) -> threading.Thread | None:
    """Serve `dist_dir` on 127.0.0.1:port in a background thread. No-op if dist missing."""
    if not dist_dir.is_dir():
        return None
    handler = functools.partial(_SpaHandler, directory=str(dist_dir.resolve()))
    # functools.partial can't set a class attribute; bind the lookup on the class
    # (one server per process, so this is not shared state in practice).
    _SpaHandler.ui_lookup = staticmethod(ui_lookup) if ui_lookup else None
    httpd = _ThreadingServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="xu-web", daemon=True)
    thread.start()
    print(f"[xu-brain] webui http://127.0.0.1:{port} (root {dist_dir.resolve()})", flush=True)
    return thread
