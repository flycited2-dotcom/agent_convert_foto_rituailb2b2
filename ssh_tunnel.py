"""SSH-туннель через paramiko: локальный порт → SSH → remote_host:remote_port.

Используется remote_agent.py и agent_watchdog.py — никаких открытых портов
на VPS не нужно.
"""
from __future__ import annotations

import logging
import select
import socket
import threading

import paramiko

log = logging.getLogger("ssh_tunnel")


def _forward_handler(local_sock: socket.socket, transport: paramiko.Transport,
                     remote_host: str, remote_port: int) -> None:
    try:
        chan = transport.open_channel(
            "direct-tcpip", (remote_host, remote_port), local_sock.getpeername()
        )
    except Exception as e:
        log.debug("Не удалось открыть канал: %s", e)
        local_sock.close()
        return

    try:
        while True:
            r, _, _ = select.select([local_sock, chan], [], [], 2)
            if local_sock in r:
                data = local_sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                local_sock.sendall(data)
    except Exception:
        pass
    finally:
        local_sock.close()
        chan.close()


class SSHTunnel:
    """Локальный порт → SSH → remote_host:remote_port."""

    def __init__(self, ssh_host: str, ssh_user: str, ssh_pass: str,
                 remote_host: str, remote_port: int) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(ssh_host, username=ssh_user, password=ssh_pass,
                              timeout=15, banner_timeout=30)
        transport = self._client.get_transport()
        transport.set_keepalive(10)

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(10)
        self.local_port: int = self._server.getsockname()[1]

        self._transport = transport
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._active = True

        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()

    def _accept_loop(self) -> None:
        while self._active:
            try:
                self._server.settimeout(1)
                try:
                    sock, _ = self._server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=_forward_handler,
                    args=(sock, self._transport, self._remote_host, self._remote_port),
                    daemon=True,
                ).start()
            except Exception:
                break

    def close(self) -> None:
        self._active = False
        try:
            self._server.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "SSHTunnel":
        return self

    def __exit__(self, *_) -> None:
        self.close()
