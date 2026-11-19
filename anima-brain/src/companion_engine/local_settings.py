"""Open settings on the engine computer; never relay the local token through MCP."""

import secrets
import socket
import threading
import time
import webbrowser


class LocalSettings:
    def __init__(self, runtime):
        self.runtime = runtime
        self._lock = threading.Lock()
        self._server = self._thread = self._socket = None
        self._url = None

    def open(self):
        with self._lock:
            if self._server is None:
                try:
                    import uvicorn

                    from .http_server import create_app
                except ImportError:
                    raise ValueError(
                        'Install the optional settings transport: pip install -e ".[http]"'
                    ) from None
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                token = secrets.token_urlsafe(32)
                app = create_app(
                    self.runtime,
                    token,
                    port=port,
                    include_mcp=False,
                    start_worker=False,
                    settings_only=True,
                )
                server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
                thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
                thread.start()
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.03)
                if not server.started:
                    server.should_exit = True
                    sock.close()
                    raise ValueError("Local settings could not start")
                self._server, self._thread, self._socket = server, thread, sock
                self._url = f"http://127.0.0.1:{port}/settings#token={token}"
            # This URL only goes to the local OS browser, not tool results or host UI state.
            if not webbrowser.open(self._url):
                raise ValueError(
                    "Could not open a browser on the engine computer; "
                    "configure COMPANION_* environment variables before starting the engine"
                )
        return {"opened_on_engine_computer": True, "keys_shared_with_chat": False}

    def close(self):
        with self._lock:
            if self._server:
                self._server.should_exit = True
                self._thread.join(timeout=10)
                self._socket.close()
                self._server = self._thread = self._socket = self._url = None
