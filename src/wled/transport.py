"""UDP senders: a real one and an in-memory fake for tests."""

from __future__ import annotations
import socket
import threading


class UdpTransport:
    """Sends datagrams over one shared UDP socket. Send errors are swallowed (a board may be offline)."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._lock = threading.Lock()
        self.errors = 0

    def send(self, host: str, port: int, datagrams: list[bytes]) -> None:
        with self._lock:
            for d in datagrams:
                try:
                    self._sock.sendto(d, (host, port))
                except OSError:
                    self.errors += 1

    def close(self) -> None:
        self._sock.close()


class FakeTransport:
    """Records what would have been sent: ``sent`` is a list of (host, port, datagram)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, int, bytes]] = []
        self._lock = threading.Lock()

    def send(self, host: str, port: int, datagrams: list[bytes]) -> None:
        with self._lock:
            self.sent.extend((host, port, d) for d in datagrams)

    def close(self) -> None:
        pass

    def clear(self) -> None:
        with self._lock:
            self.sent.clear()
