"""Conversational launcher for bounded, authorized validation workers.

The launcher owns scope parsing, subprocess lifecycle, console serialization,
and approval messages. It does not implement browser actions. A worker is a
separate executable speaking the small line protocol documented below.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import UTC, datetime
import hashlib
import json
import os
import shlex
import signal
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from enterprise_stealth_range import AuthorizationScenarioAdapter, ConfigurationError

ADMIN_PROMPT = "AdminConsole> "
ALERT_PROMPT = (
    "[ALERT] Verification checkpoint reached. Vulnerability fingerprint "
    "detected. Do you authorize running the separate read-only authorization "
    "check? [Yes/No]"
)
RUNTIME_COMMANDS = frozenset({"status", "pause", "resume", "cancel"})
EVENT_PREFIX = "SUPERVISOR_EVENT:"


class LauncherError(ValueError):
    """An operator command or worker protocol record is invalid."""


class CampaignState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ParsedCampaign:
    payload: dict[str, str | None]

    @property
    def mode(self) -> str:
        return "authenticated" if self.payload["username"] is not None else "unauthenticated"


def parse_campaign_command(
    text: str, *, auth_entry_route: str = "/login", role_profile: str = "standard_user"
) -> ParsedCampaign:
    """Parse exactly the two supported ``test`` command forms."""
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as error:
        raise LauncherError("invalid quoting in test command") from error
    if len(tokens) < 2 or tokens[0].lower() != "test":
        raise LauncherError("expected: test URL [user=USER pass=PASS]")
    target = tokens[1]
    parameters: dict[str, str] = {}
    for token in tokens[2:]:
        key, separator, value = token.partition("=")
        if not separator or key not in {"user", "pass"} or not value or key in parameters:
            raise LauncherError("only one non-empty user= and pass= pair is supported")
        parameters[key] = value
    if not parameters:
        payload: dict[str, str | None] = {
            "target_scope_url": target,
            "username": None,
            "password": None,
            "auth_entry_route": None,
            "role_profile": None,
        }
        AuthorizationScenarioAdapter.from_payload({"target_scope_url": target})
        return ParsedCampaign(payload)
    if set(parameters) != {"user", "pass"}:
        raise LauncherError("authenticated mode requires both user= and pass=")
    payload = {
        "target_scope_url": target,
        "username": parameters["user"],
        "password": parameters["pass"],
        "auth_entry_route": auth_entry_route,
        "role_profile": role_profile,
    }
    AuthorizationScenarioAdapter.from_payload(payload)
    return ParsedCampaign(payload)


def write_campaign_scope(campaign: ParsedCampaign, path: Path) -> None:
    """Atomically replace the local scope file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(campaign.payload, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class AsyncConsole(Protocol):
    async def read(self, prompt: str) -> str: ...
    async def write(self, message: str) -> None: ...


class TerminalConsole:
    """Serialize terminal I/O while keeping blocking input outside the event loop."""

    def __init__(self) -> None:
        self._output_lock = asyncio.Lock()

    async def read(self, prompt: str) -> str:
        return await asyncio.to_thread(input, prompt)

    async def write(self, message: str) -> None:
        async with self._output_lock:
            print(message, flush=True)


class ConversationalLauncher:
    """Persistent console and one-at-a-time subprocess supervisor."""

    def __init__(
        self,
        *,
        console: AsyncConsole,
        scope_path: Path = Path("campaign_scope.json"),
        session_root: Path = Path("/dev/shm/session_core"),
        report_path: Path = Path("CLIENT_REPORT.md"),
        logs_path: Path = Path("logs"),
        worker_command: tuple[str, ...] | None = None,
    ) -> None:
        self.console = console
        self.scope_path = scope_path
        self.session_root = session_root
        self.report_path = report_path
        self.logs_path = logs_path
        self.logs_path.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.logs_path / "audit_trail.csv"
        self.worker_command = worker_command or (
            sys.executable,
            str(Path(__file__).with_name("campaign_session_manager.py")),
            "--inspect",
            str(scope_path),
        )
        self.state = CampaignState.IDLE
        self.process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task[None] | None = None
        self._campaign: ParsedCampaign | None = None
        self._approval_lock = asyncio.Lock()
        self._telemetry: list[str] = []
        self._finalized_processes: set[int] = set()
        self._termination_outcomes: dict[int, str] = {}

    async def run(self) -> None:
        await self.console.write("Bounded validation console ready. Type 'quit' to exit.")
        while True:
            try:
                text = (await self.console.read(ADMIN_PROMPT)).strip()
            except (EOFError, KeyboardInterrupt):
                text = "quit"
            if not text:
                continue
            if self.state is CampaignState.APPROVAL_REQUIRED:
                await self._handle_approval(text)
            elif text.lower() in {"quit", "exit"}:
                await self.cancel()
                return
            elif text.lower() in RUNTIME_COMMANDS:
                await self._runtime_command(text.lower())
            elif text.lower().startswith("test "):
                if self.process is not None and self.process.returncode is None:
                    await self.console.write(
                        "[SCOPE WARNING] Target scope cannot be modified during an active campaign."
                    )
                    continue
                try:
                    campaign = parse_campaign_command(text)
                except (LauncherError, ConfigurationError) as error:
                    await self.console.write(f"Command rejected: {error}")
                    continue
                await self._launch(campaign)
            else:
                await self.console.write("Unknown command. Use test, status, pause, resume, cancel, or quit.")

    async def _launch(self, campaign: ParsedCampaign) -> None:
        write_campaign_scope(campaign, self.scope_path)
        self.session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.session_root.chmod(0o700)
        self._campaign = campaign
        self._telemetry = []
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.worker_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException as error:
            self._telemetry.append(f"launcher_error: {type(error).__name__}: {error}")
            self._finalize_campaign(id(campaign), "launch_anomaly")
            raise
        self.state = CampaignState.RUNNING
        self._output_task = asyncio.create_task(self._relay_worker_output())
        await self.console.write(f"Campaign started in {campaign.mode} mode.")

    async def _relay_worker_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        process = self.process
        outcome = "runtime_anomaly"
        try:
            while line := await process.stdout.readline():
                message = line.decode("utf-8", errors="replace").rstrip()
                self._telemetry.append(message)
                if message.startswith(EVENT_PREFIX):
                    await self._checkpoint(message.removeprefix(EVENT_PREFIX).strip())
                else:
                    await self.console.write(f"[worker] {message}")
            returncode = await process.wait()
            outcome = self._termination_outcomes.pop(
                process.pid, f"worker_exit_{returncode}"
            )
        except BaseException as error:
            self._telemetry.append(f"relay_error: {type(error).__name__}: {error}")
            outcome = self._termination_outcomes.pop(process.pid, outcome)
            raise
        finally:
            self._finalize_campaign(process.pid, outcome)
        if self.process is process and self.state not in {
            CampaignState.CANCELLED,
            CampaignState.APPROVAL_REQUIRED,
        }:
            self.state = CampaignState.IDLE
            await self.console.write(f"Campaign worker exited with status {returncode}.")

    async def _checkpoint(self, fingerprint: str) -> None:
        async with self._approval_lock:
            if self.process is None or self.process.returncode is not None:
                return
            os.killpg(self.process.pid, signal.SIGSTOP)
            self.state = CampaignState.APPROVAL_REQUIRED
            safe = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
            await self.console.write(f"\033[1m{ALERT_PROMPT}\033[0m [classification={safe}]")

    async def _handle_approval(self, text: str) -> None:
        answer = text.strip().lower()
        if answer in {"yes", "y"}:
            await self._send_worker("VERIFY_READ_ONLY")
            await self.resume()
        elif answer in {"no", "n"}:
            await self.cancel()
            self._wipe_session_cache()
            self._write_redacted_report("Read-only verification declined by administrator.")
            await self.console.write("Campaign cancelled; redacted CLIENT_REPORT.md generated.")
        else:
            await self.console.write("Approval response must be Yes/Y or No/N.")

    async def _runtime_command(self, command: str) -> None:
        if command == "status":
            await self.console.write(f"Campaign status: {self.state.value}")
        elif command == "pause":
            await self.pause()
        elif command == "resume":
            await self.resume()
        elif command == "cancel":
            await self.cancel()

    async def pause(self) -> None:
        if self.process is None or self.process.returncode is not None:
            await self.console.write("No active campaign.")
            return
        if self.state is CampaignState.APPROVAL_REQUIRED:
            await self.console.write("Campaign is held at a verification checkpoint.")
            return
        os.killpg(self.process.pid, signal.SIGSTOP)
        self.state = CampaignState.PAUSED
        await self.console.write("Campaign paused.")

    async def resume(self) -> None:
        if self.process is None or self.process.returncode is not None:
            await self.console.write("No active campaign.")
            return
        os.killpg(self.process.pid, signal.SIGCONT)
        await self._send_worker("RESUME")
        self.state = CampaignState.RUNNING
        await self.console.write("Campaign resumed.")

    async def cancel(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            self.state = CampaignState.IDLE
            return
        os.killpg(process.pid, signal.SIGCONT)
        self._termination_outcomes[process.pid] = "cancelled"
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        self.state = CampaignState.CANCELLED
        self._wipe_session_cache()
        self._finalize_campaign(process.pid, "cancelled")

    async def _send_worker(self, command: str) -> None:
        if self.process is None or self.process.stdin is None or self.process.stdin.is_closing():
            return
        self.process.stdin.write(f"{command}\n".encode())
        await self.process.stdin.drain()

    def _wipe_session_cache(self) -> None:
        if self.session_root.resolve() != Path("/dev/shm/session_core"):
            return
        try:
            self.session_root.rmdir()  # leases are unlinked; refuse unknown files
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _write_redacted_report(self, outcome: str) -> None:
        target_hash = "unavailable"
        mode = "unavailable"
        if self._campaign is not None:
            target = str(self._campaign.payload["target_scope_url"])
            target_hash = hashlib.sha256(target.encode()).hexdigest()
            mode = self._campaign.mode
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            "# Client report\n\n"
            f"- Campaign mode: `{mode}`\n"
            f"- Target classification: `{target_hash}`\n"
            f"- Outcome: {outcome}\n\n"
            "Credentials, URLs, cookies, tokens, and response bodies were redacted.\n",
            encoding="utf-8",
        )

    def _finalize_campaign(self, process_id: int, outcome: str) -> None:
        """Synchronously persist the audit and report before control is returned."""
        if process_id in self._finalized_processes:
            return
        self._finalized_processes.add(process_id)
        timestamp = datetime.now(UTC).isoformat()
        telemetry = "\n".join(self._telemetry) or "(no worker telemetry)"
        tracking_hash = hashlib.sha256(
            f"{timestamp}\0{process_id}\0{outcome}\0{telemetry}".encode()
        ).hexdigest()
        new_file = not self.audit_path.exists()
        with self.audit_path.open("a", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            if new_file:
                writer.writerow(("timestamp", "tracking_hash", "outcome", "telemetry"))
            writer.writerow((timestamp, tracking_hash, outcome, telemetry))
            output.flush()
            os.fsync(output.fileno())

        route = "/"
        mode = "unavailable"
        parameter_names = "none"
        target_hash = "unavailable"
        if self._campaign is not None:
            target = str(self._campaign.payload["target_scope_url"])
            route = urlsplit(target).path or "/"
            target_hash = hashlib.sha256(target.encode()).hexdigest()
            mode = self._campaign.mode
            if mode == "authenticated":
                auth = str(self._campaign.payload["auth_entry_route"])
                route = f"{route}, {urlsplit(auth).path or '/'}"
                parameter_names = "username, password, role_profile"
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            "# Client report\n\n"
            "## Executive summary\n\n"
            f"Authorized `{mode}` validation ended with `{outcome}`.\n\n"
            "## Coverage\n\n"
            f"- Checked sub-routes: `{route}`\n"
            f"- Parameters evaluated: `{parameter_names}`\n"
            f"- Target classification: `{target_hash}`\n\n"
            "## Tracking\n\n"
            f"- Timestamp (UTC): `{timestamp}`\n"
            f"- Tracking hash: `{tracking_hash}`\n\n"
            "Credentials, hosts, cookies, tokens, and response bodies are not included.\n",
            encoding="utf-8",
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=Path("campaign_scope.json"))
    parser.add_argument("--report", type=Path, default=Path("CLIENT_REPORT.md"))
    parser.add_argument("--worker-command", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    arguments = build_argument_parser().parse_args()
    command = tuple(arguments.worker_command) if arguments.worker_command else None
    launcher = ConversationalLauncher(
        console=TerminalConsole(),
        scope_path=arguments.scope,
        report_path=arguments.report,
        worker_command=command,
    )
    asyncio.run(launcher.run())


if __name__ == "__main__":
    main()
