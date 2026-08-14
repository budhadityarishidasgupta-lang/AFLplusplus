"""Central asynchronous pause gate and loopback-only live command listener."""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

CONFIRMATION_PROMPT = (
    "VULNERABILITY ANOMALY DETECTED. Do you authorize running a separate, "
    "read-only verification check? [Yes/No]"
)
MAX_COMMAND_BYTES = 256
MAX_JITTER_MS = 15_000


class PauseReason(str, Enum):
    RESPONSE_ANOMALY = "RESPONSE_ANOMALY"
    BOUNDARY_CHECK_FAILED = "BOUNDARY_CHECK_FAILED"
    VALIDATION_BUDGET_FINISHED = "VALIDATION_BUDGET_FINISHED"


class SupervisorState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class SystemPauseEvent:
    reason: PauseReason
    source: str
    detail: str = ""


@dataclass(frozen=True)
class LiveCommand:
    name: str
    jitter_ms: int | None = None


class CommandAudit(Protocol):
    def __call__(self, event: str, detail: str) -> None: ...


def parse_live_command(text: str) -> LiveCommand | None:
    """Parse the four-command text grammar; every other input is dropped."""
    normalized = text.strip()
    if normalized in {"PAUSE", "CANCEL", "STATUS_REPORT"}:
        return LiveCommand(normalized)
    prefix = "ADJUST_SPEED_JITTER="
    if normalized.startswith(prefix):
        value = normalized.removeprefix(prefix)
        if value.isascii() and value.isdecimal():
            jitter = int(value)
            if 0 <= jitter <= MAX_JITTER_MS:
                return LiveCommand("ADJUST_SPEED_JITTER", jitter)
    return None


class LiveCommandChannel:
    """Background TCP listener that forwards validated commands to an event loop.

    The socket is always bound to literal loopback. Each connection may submit
    one UTF-8 command terminated by EOF or a newline. No target-changing grammar
    exists, so URL/domain input is rejected before reaching the supervisor.
    """

    def __init__(
        self,
        on_command: Callable[[LiveCommand], None],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        audit: CommandAudit | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("LiveCommandChannel must bind to literal 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer in [0, 65535]")
        self._host, self._requested_port = host, port
        self._on_command = on_command
        self._audit = audit or (lambda _event, _detail: None)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self.port: int | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("LiveCommandChannel is already started")
        self._thread = threading.Thread(
            target=self._serve, name="LiveCommandChannel", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=2):
            raise RuntimeError("LiveCommandChannel failed to start")

    def _serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self._socket = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._requested_port))
            listener.listen(4)
            listener.settimeout(0.2)
            self.port = listener.getsockname()[1]
            self._ready.set()
            while not self._stop.is_set():
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with connection:
                    connection.settimeout(1)
                    raw = self._receive(connection)
                self._dispatch(raw)

    @staticmethod
    def _receive(connection: socket.socket) -> bytes:
        data = bytearray()
        while len(data) <= MAX_COMMAND_BYTES:
            try:
                chunk = connection.recv(64)
            except socket.timeout:
                break
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        return bytes(data)

    def _dispatch(self, raw: bytes) -> None:
        if len(raw) > MAX_COMMAND_BYTES:
            self._audit("command_dropped", "oversized")
            return
        try:
            text = raw.split(b"\n", 1)[0].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self._audit("command_dropped", "invalid_utf8")
            return
        command = parse_live_command(text)
        if command is None:
            self._audit("command_dropped", "outside_allowlist")
            return
        self._audit("command_accepted", command.name)
        self._on_command(command)

    def close(self) -> None:
        self._stop.set()
        current = self._socket
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            current.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> LiveCommandChannel:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SystemSupervisor:
    """Serialize worker pause events and hold the primary execution gate."""

    def __init__(
        self,
        *,
        prompt: Callable[[str], Awaitable[str]],
        read_only_verification: Callable[[SystemPauseEvent], Awaitable[None]],
        status_reporter: Callable[[], None] | None = None,
        audit: CommandAudit | None = None,
    ) -> None:
        self._prompt = prompt
        self._verify = read_only_verification
        self._status_reporter = status_reporter or (lambda: None)
        self._audit = audit or (lambda _event, _detail: None)
        self._events: asyncio.Queue[SystemPauseEvent | None] = asyncio.Queue()
        self._execution_gate = asyncio.Event()
        self._execution_gate.set()
        self._cancelled = asyncio.Event()
        self._monitor: asyncio.Task[None] | None = None
        self._approval_active = False
        self.state = SupervisorState.RUNNING
        self.speed_jitter_ms = 0
        self.channel: LiveCommandChannel | None = None

    async def start(self, *, command_port: int = 0) -> None:
        if self._monitor is not None:
            raise RuntimeError("SystemSupervisor is already started")
        loop = asyncio.get_running_loop()
        self.channel = LiveCommandChannel(
            lambda command: loop.call_soon_threadsafe(self._handle_command, command),
            port=command_port,
            audit=self._audit,
        )
        self.channel.start()
        self._monitor = asyncio.create_task(self._monitor_events())

    async def publish_pause(self, event: SystemPauseEvent) -> None:
        if self.state in {SupervisorState.CANCELLED, SupervisorState.STOPPED}:
            return
        self._execution_gate.clear()
        await self._events.put(event)

    async def wait_until_runnable(self) -> None:
        """Primary execution awaits this checkpoint between bounded actions."""
        await self._execution_gate.wait()
        if self._cancelled.is_set():
            raise asyncio.CancelledError("campaign cancelled by administrator")

    async def _monitor_events(self) -> None:
        while True:
            event = await self._events.get()
            if event is None:
                return
            self.state = SupervisorState.PAUSED
            self._approval_active = True
            self._audit("system_pause", f"{event.reason.value}:{event.source}")
            try:
                answer = (await self._prompt(CONFIRMATION_PROMPT)).strip().lower()
                if answer in {"yes", "y"}:
                    self._audit("verification_decision", "approved")
                    await self._verify(event)
                else:
                    self._audit("verification_decision", "denied")
                if self.state is not SupervisorState.CANCELLED:
                    self.state = SupervisorState.RUNNING
                    self._execution_gate.set()
            finally:
                self._approval_active = False

    def _handle_command(self, command: LiveCommand) -> None:
        if command.name == "PAUSE":
            self.state = SupervisorState.PAUSED
            self._execution_gate.clear()
        elif command.name == "CANCEL":
            self.state = SupervisorState.CANCELLED
            self._cancelled.set()
            self._execution_gate.set()
        elif command.name == "STATUS_REPORT":
            self._status_reporter()
        elif command.name == "ADJUST_SPEED_JITTER":
            assert command.jitter_ms is not None
            self.speed_jitter_ms = command.jitter_ms

    def resume_admin_pause(self) -> None:
        if self.state is SupervisorState.PAUSED and not self._approval_active:
            self.state = SupervisorState.RUNNING
            self._execution_gate.set()

    async def close(self) -> None:
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        if self._monitor is not None:
            await self._events.put(None)
            await self._monitor
            self._monitor = None
        self.state = SupervisorState.STOPPED
        self._execution_gate.set()
