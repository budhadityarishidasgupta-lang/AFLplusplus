import asyncio
import socket
import unittest

from system_supervisor import (
    CONFIRMATION_PROMPT,
    LiveCommandChannel,
    PauseReason,
    SupervisorState,
    SystemPauseEvent,
    SystemSupervisor,
    parse_live_command,
)


class LiveCommandTests(unittest.TestCase):
    def test_parser_accepts_only_documented_commands(self):
        self.assertEqual(parse_live_command("PAUSE").name, "PAUSE")
        self.assertEqual(parse_live_command("CANCEL").name, "CANCEL")
        self.assertEqual(parse_live_command("STATUS_REPORT").name, "STATUS_REPORT")
        adjusted = parse_live_command("ADJUST_SPEED_JITTER=1250")
        self.assertEqual((adjusted.name, adjusted.jitter_ms), ("ADJUST_SPEED_JITTER", 1250))
        for rejected in (
            "TARGET_URL=https://example.com",
            "CHANGE_DOMAIN example.com",
            "ADJUST_SPEED_JITTER=-1",
            "ADJUST_SPEED_JITTER=15001",
            "status_report",
        ):
            with self.subTest(rejected=rejected):
                self.assertIsNone(parse_live_command(rejected))

    def test_socket_listener_drops_scope_change(self):
        received, audit = [], []
        channel = LiveCommandChannel(received.append, audit=lambda event, detail: audit.append((event, detail)))
        channel.start()
        try:
            for text in ("TARGET_URL=http://127.0.0.1:9999\n", "PAUSE\n"):
                with socket.create_connection(("127.0.0.1", channel.port), timeout=1) as client:
                    client.sendall(text.encode())
            for _ in range(50):
                if received:
                    break
                asyncio.run(asyncio.sleep(0.01))
        finally:
            channel.close()
        self.assertEqual([command.name for command in received], ["PAUSE"])
        self.assertIn(("command_dropped", "outside_allowlist"), audit)

    def test_listener_rejects_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            LiveCommandChannel(lambda _command: None, host="0.0.0.0")


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_event_gates_execution_and_prompts(self):
        prompts, verified = [], []

        async def prompt(message):
            prompts.append(message)
            await asyncio.sleep(0.01)
            return "Yes"

        async def verify(event):
            verified.append(event)

        supervisor = SystemSupervisor(prompt=prompt, read_only_verification=verify)
        await supervisor.start()
        try:
            event = SystemPauseEvent(PauseReason.RESPONSE_ANOMALY, "browser-01")
            await supervisor.publish_pause(event)
            waiter = asyncio.create_task(supervisor.wait_until_runnable())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            await asyncio.wait_for(waiter, timeout=1)
            self.assertEqual(prompts, [CONFIRMATION_PROMPT])
            self.assertEqual(verified, [event])
            self.assertEqual(supervisor.state, SupervisorState.RUNNING)
        finally:
            await supervisor.close()

    async def test_denial_skips_read_only_verification(self):
        verified = []

        async def prompt(_message):
            return "No"

        async def verify(event):
            verified.append(event)

        supervisor = SystemSupervisor(prompt=prompt, read_only_verification=verify)
        await supervisor.start()
        try:
            await supervisor.publish_pause(
                SystemPauseEvent(PauseReason.VALIDATION_BUDGET_FINISHED, "browser-01")
            )
            await asyncio.wait_for(supervisor.wait_until_runnable(), timeout=1)
            self.assertEqual(verified, [])
        finally:
            await supervisor.close()


if __name__ == "__main__":
    unittest.main()
