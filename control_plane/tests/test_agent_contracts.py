from __future__ import annotations

import subprocess
import sys
import unittest

from control_plane.agent_contracts import (
    AgentContract,
    AgentNotFoundError,
    CapabilityNotFoundError,
    CapabilityRegistry,
    CapabilityStatus,
    ContractError,
    DuplicateAgentError,
    TaskContract,
    ValidationError,
)


def contract(agent_id: str = "reviewer", capabilities: tuple[str, ...] = ("review",)) -> AgentContract:
    return AgentContract(
        agent_id=agent_id,
        purpose="Review declarative input",
        version="1.2.3",
        capabilities=capabilities,
        supported_tasks=("inspect",),
        inputs={"document": "text to inspect"},
        outputs={"summary": "review summary"},
        permissions=("read_only",),
    )


class AgentContractTests(unittest.TestCase):
    def test_contracts_validate_and_copy_mappings(self) -> None:
        inputs = {"document": "text"}
        created = contract()
        self.assertEqual(created.inputs["document"], "text to inspect")
        self.assertEqual(created.capabilities, ("review",))
        with self.assertRaises(TypeError):
            created.inputs["document"] = "changed"  # type: ignore[index]
        self.assertEqual(
            TaskContract("task-1", "Review this document", "review", {"scope": "local"}, ("human",), "summary").task_id,
            "task-1",
        )
        self.assertEqual(inputs, {"document": "text"})

    def test_invalid_contract_and_task_fields_are_rejected_deterministically(self) -> None:
        with self.assertRaisesRegex(ValidationError, "semantic version"):
            AgentContract("reviewer", "Review", "latest", ("review",), (), {}, {})
        with self.assertRaisesRegex(ValidationError, "at least one"):
            AgentContract("reviewer", "Review", "1.0.0", (), (), {}, {})
        with self.assertRaisesRegex(ValidationError, "task_id"):
            TaskContract("bad id", "Do work", "review", expected_output="result")

    def test_registration_is_metadata_not_readiness_or_authorization(self) -> None:
        registry = CapabilityRegistry()
        registration = registry.register(contract())
        self.assertEqual(registration.status, CapabilityStatus.DISCOVERED)
        self.assertFalse(registration.execution_ready)
        self.assertFalse(registration.authorized)
        with self.assertRaisesRegex(ValidationError, "cannot grant"):
            from control_plane.agent_contracts import AgentRegistration

            AgentRegistration(contract(), execution_ready=True)

    def test_duplicate_registration_and_discovery_errors(self) -> None:
        registry = CapabilityRegistry()
        registry.register(contract())
        with self.assertRaises(DuplicateAgentError):
            registry.register(contract())
        with self.assertRaises(AgentNotFoundError):
            registry.discover("unknown")

    def test_capability_query_list_and_unregister_are_deterministic(self) -> None:
        registry = CapabilityRegistry()
        registry.register(contract("zeta", ("review", "summarize")))
        registry.register(contract("alpha", ("review",)))
        self.assertEqual([item.contract.agent_id for item in registry.query("review")], ["alpha", "zeta"])
        self.assertEqual([item.contract.agent_id for item in registry.list_agents()], ["alpha", "zeta"])
        self.assertEqual(registry.unregister("alpha").contract.agent_id, "alpha")
        self.assertEqual([item.contract.agent_id for item in registry.query("review")], ["zeta"])
        with self.assertRaises(CapabilityNotFoundError):
            registry.query("missing")
        with self.assertRaises(AgentNotFoundError):
            registry.unregister("alpha")

    def test_all_capability_status_transitions_preserve_non_readiness(self) -> None:
        registry = CapabilityRegistry()
        registry.register(contract())
        for status in CapabilityStatus:
            updated = registry.set_status("reviewer", status)
            self.assertIs(updated.status, status)
            self.assertFalse(updated.execution_ready)
            self.assertFalse(updated.authorized)

    def test_import_isolation_does_not_load_legacy_agents(self) -> None:
        code = (
            "import sys; import control_plane.agent_contracts; "
            "assert 'multi_agent_system' not in sys.modules; "
            "assert 'conversational_launcher' not in sys.modules; "
            "assert 'enterprise_stealth_range' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
