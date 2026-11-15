"""Zero-polling socket activator that starts media_grab.web on demand."""

import argparse
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional


def _code_mtime() -> float:
    base = Path(__file__).resolve().parent
    latest = 0.0
    for pattern in ("*.py", "extractors/*.py"):
        for fp in base.glob(pattern):
            try:
                latest = max(latest, fp.stat().st_mtime)
            except OSError:
                pass
    return latest


def _can_connect(host: str, port: int, timeout: float = 0.25) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _shutdown_worker(port: int) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6) as sock:
            sock.sendall(
                b"POST /api/shutdown HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            sock.settimeout(0.6)
            sock.recv(1024)
    except OSError:
        pass


def _default_spawn(worker_port: int, idle_timeout: int) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "media_grab.web",
        "--host",
        "127.0.0.1",
        "--port",
        str(worker_port),
        "--idle-timeout",
        str(idle_timeout),
        "--allow-shutdown",
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _pipe_stream(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class OnDemandGate:
    """Listens on a fixed local port and spawns/forwards to a lazy web worker."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        worker_port: int = 8766,
        idle_timeout: int = 180,
        spawn_fn: Optional[Callable[[], Any]] = None,
    ):
        self.host = host
        self.worker_port = worker_port
        self.idle_timeout = idle_timeout
        self._spawn_fn = spawn_fn or (lambda: _default_spawn(worker_port, idle_timeout))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker_mtime: Optional[float] = None

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(64)
        self._srv = srv
        self.port: int = srv.getsockname()[1]

    def _spawn_and_wait_locked(self) -> None:
        self._worker_mtime = _code_mtime()
        self._spawn_fn()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._stop.is_set():
            if _can_connect("127.0.0.1", self.worker_port, timeout=0.15):
                return
            time.sleep(0.04)
        raise TimeoutError(f"Worker failed to listen on 127.0.0.1:{self.worker_port}")

    def ensure_worker(self) -> None:
        with self._lock:
            now_mtime = _code_mtime()
            if self._worker_mtime is not None and now_mtime > self._worker_mtime:
                if _can_connect("127.0.0.1", self.worker_port, timeout=0.2):
                    _shutdown_worker(self.worker_port)
                    end_wait = time.monotonic() + 1.5
                    while time.monotonic() < end_wait and _can_connect("127.0.0.1", self.worker_port, timeout=0.1):
                        time.sleep(0.05)
                self._spawn_and_wait_locked()
                return

            if _can_connect("127.0.0.1", self.worker_port):
                if self._worker_mtime is None:
                    self._worker_mtime = now_mtime
                return
            self._spawn_and_wait_locked()

    def _connect_or_spawn(self) -> socket.socket:
        now_mtime = _code_mtime()
        if self._worker_mtime is None or now_mtime <= self._worker_mtime:
            try:
                sock = socket.create_connection(("127.0.0.1", self.worker_port), timeout=0.35)
                if self._worker_mtime is None:
                    self._worker_mtime = now_mtime
                return sock
            except OSError:
                pass
        self.ensure_worker()
        return socket.create_connection(("127.0.0.1", self.worker_port), timeout=5.0)

    def _handle_client(self, client_sock: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            upstream = self._connect_or_spawn()
            client_sock.settimeout(None)
            upstream.settimeout(None)

            t1 = threading.Thread(target=_pipe_stream, args=(client_sock, upstream), daemon=True)
            t2 = threading.Thread(target=_pipe_stream, args=(upstream, client_sock), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        except Exception:
            pass
        finally:
            for s in (client_sock, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                client_sock, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="On-demand socket activator for media_grab.web")
    parser.add_argument("--host", default="127.0.0.1", help="Gate listen host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Gate listen port (default 8765)")
    parser.add_argument("--worker-port", type=int, default=8766, help="Lazy web worker port (default 8766)")
    parser.add_argument("--idle-timeout", type=int, default=180, help="Worker idle timeout in seconds (default 180)")
    args = parser.parse_args(argv)

    gate = OnDemandGate(
        host=args.host,
        port=args.port,
        worker_port=args.worker_port,
        idle_timeout=args.idle_timeout,
    )
    print(f"[media-grab] 按需网关已就绪: http://{args.host}:{gate.port} -> worker:{args.worker_port}")
    try:
        gate.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gate.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
