"""Owner-supervised simulation controls for persisted Studio missions.

Rendering is read-only. Durable authorization and submission occur only from
separate explicit button actions with the process-issued owner authority.
"""
from __future__ import annotations

import re
from pathlib import Path


def route_explicit_execution_request(text: str) -> bool:
    """Recognize a request to inspect execution, never a request to execute it."""
    return isinstance(text, str) and bool(re.search(
        r"\b(execution status|simulation status|reconcile|check the simulation|request synthetic execution)\b", text, re.I))


def _execution_store_path(data_dir, db_path):
    if data_dir is not None:
        return Path(data_dir) / "mission-execution.sqlite3"
    if db_path is not None:
        return Path(db_path).with_name("mission-execution.sqlite3")
    from . import studio
    return Path(studio.DATA) / "mission-execution.sqlite3"


def _load_state(conversation, mission_conversation, mission_controller, session_id, data_dir, db_path, discovery,
                owner_authority=None):
    repository = mission_controller.ConversationMissionRepository(
        conversation, data_dir=data_dir, db_path=db_path,
        synthetic_fixture_discovery=discovery)
    if discovery is None and owner_authority is not None:
        owner_context = getattr(owner_authority, "_service", None)
        if owner_context is not None:
            discovery = owner_context.mission_capability_discovery(
                _owner_authority=owner_authority)
            repository.synthetic_fixture_discovery = discovery
            from . import owner_simulation
            mission_conversation.owner_simulation = owner_simulation
            repository.backend = conversation
    loaded = repository.load(session_id)
    if loaded is None:
        return repository, None, None
    state, request_key = loaded
    if getattr(state, "synthetic_acceptance", False) and owner_authority is not None:
        state._trusted_owner_simulation = True
        state._owner_authority = owner_authority
        state.capability_discovery = discovery or repository.synthetic_fixture_discovery
    return repository, state, request_key


def _persist(repository, session_id, state, request_key):
    if not repository.persist(session_id, state, request_key):
        raise RuntimeError("Mission state could not be durably saved; no further action is allowed.")


def render(st, conversation, mission_conversation, mission_controller, mission_execution,
           session_id, data_dir, db_path, *, owner_authority=None, service_factory=None):
    """Render a mission in read-only mode unless externally injected authority exists."""
    if owner_authority is None or service_factory is None:
        repository, state, request_key = _load_state(
            conversation, mission_conversation, mission_controller, session_id,
            data_dir, db_path, discovery=None, owner_authority=owner_authority)
        if state is None:
            return
        state.synthetic_acceptance = False
        st.subheader("Mission proposal and review")
        st.info(mission_conversation.describe_state(state))
        st.info("Synthetic acceptance capability is unavailable in ordinary Studio. This mission is review-only; no action was taken.")
        st.warning("Owner-supervised synthetic simulation is unavailable in this local session. No action was taken.")
        return

    try:
        service = service_factory()
    except Exception as error:
        st.warning("Owner simulation is unavailable in this local session: " + str(error))
        return
    discovery = service.mission_capability_discovery(_owner_authority=owner_authority)
    repository, state, request_key = _load_state(
        conversation, mission_conversation, mission_controller, session_id,
        data_dir, db_path, discovery=discovery, owner_authority=owner_authority)
    if state is None:
        return
    state.synthetic_acceptance = True
    state._trusted_owner_simulation = True
    state._owner_authority = owner_authority
    st.subheader("Mission proposal and supervised simulation")
    st.markdown("### Mission proposed")
    st.write("**Objective:** " + state.mission.original_objective)
    st.write("**Expected outcome:** " + state.mission.intended_outcome)
    st.write("**Limitations:** This is a deterministic local fixture. It does not perform genuine web research, contact external services, verify facts, or take real-world action.")
    for task in state.plan.tasks:
        with st.container(border=True):
            st.markdown("#### Proposed specialist: Synthetic simulation specialist")
            st.write("Planned step: " + task.description)
            st.write("Expected output: " + "; ".join(task.expected_deliverables))
            st.write("Uncertainty: fixture output is illustrative and is not independently verified.")
            if task.missing_prerequisites:
                st.warning("Limitations or missing prerequisites: " + "; ".join(task.missing_prerequisites))
            planning = state.approvals.get(task.task_id)
            decision = planning[1] if planning else None
            if planning is None:
                if st.button("Request plan review", key=f"sb48-review-{task.task_id}"):
                    try:
                        mission_conversation.create_approval_request(state, task.task_id)
                        _persist(repository, session_id, state, request_key)
                        st.rerun()
                    except (ValueError, OSError, RuntimeError) as error:
                        st.error("Could not prepare the approval request: " + str(error))
                continue
            if decision is None:
                approve_col, reject_col = st.columns(2)
                if approve_col.button("Approve plan", key=f"sb48-approve-plan-{task.task_id}"):
                    try:
                        mission_conversation.decide_approval(
                            state, task.task_id, approved=True, human_identity="local-owner",
                            reason="Explicit owner approval in local Studio")
                        _persist(repository, session_id, state, request_key)
                        st.rerun()
                    except (ValueError, OSError, RuntimeError) as error:
                        st.error("Plan approval was not saved: " + str(error))
                if reject_col.button("Reject plan", key=f"sb48-reject-plan-{task.task_id}"):
                    try:
                        mission_conversation.decide_approval(
                            state, task.task_id, approved=False, human_identity="local-owner",
                            reason="Owner rejected the plan in local Studio")
                        _persist(repository, session_id, state, request_key)
                        st.rerun()
                    except (ValueError, OSError, RuntimeError) as error:
                        st.error("Plan rejection was not saved: " + str(error))
                continue
            st.caption("Planning decision: " + decision.status.value)
            if decision.status.value.lower() != "approved":
                st.info("The plan was rejected. No simulation can be authorized.")
                continue
            record = state.execution_records.get(task.task_id, {})
            status = record.get("status")
            if status is None:
                projection = service.lookup(state, task.task_id)
                status = projection.get("state")
                if status == "NOT_SUBMITTED":
                    status = None
            if status in {"completed", "failed", "cancelled", "timed_out", "interrupted", "running"}:
                _render_result(st, service, state, task.task_id, status)
                continue
            if status not in {"authorization_issued", "authorized"}:
                st.markdown("### Separate execution approval")
                st.write("If authorized, the deterministic synthetic specialist will generate an illustrative structured report from the mission text and criteria. It will not browse, contact services, or act outside this process.")
                authorize_col, cancel_col = st.columns(2)
                if authorize_col.button("Authorize synthetic simulation", key=f"sb48-authorize-{task.task_id}"):
                    try:
                        from .mission_execution import ExecutionApproval
                        approval = ExecutionApproval(
                            state.mission.mission_id, task.task_id,
                            "synthetic.brain-owner-simulation", ("read-fixture",),
                            "local-owner", "Explicit owner approval for one synthetic simulation")
                        service.authorize(state, task.task_id, approval,
                                          persist_claim=lambda value: repository.persist(
                                              session_id, value, request_key))
                        st.rerun()
                    except Exception as error:
                        st.error("Simulation authorization failed closed: " + str(error))
                if cancel_col.button("Cancel simulation request", key=f"sb48-cancel-{task.task_id}"):
                    try:
                        service.deny(state, task.task_id, "Owner cancelled before simulation authorization.")
                        _persist(repository, session_id, state, request_key)
                        st.rerun()
                    except Exception as error:
                        st.error("Cancellation could not be recorded: " + str(error))
                continue
            st.markdown("### Submit simulation")
            st.warning("Authorization alone has not submitted a task. Submission is a separate, deliberate action. The attempt can run only once.")
            submit_col, cancel_col = st.columns(2)
            if submit_col.button("Run approved simulation", key=f"sb48-submit-{task.task_id}"):
                try:
                    authorization = service._authorizations.get((state.mission.mission_id, task.task_id))
                    result = service.submit(
                        state, task.task_id, authorization,
                        persist_state=lambda value: repository.persist(session_id, value, request_key))
                    if result is not None and result.state.value == "completed":
                        service.verify(state, task.task_id, result)
                    _persist(repository, session_id, state, request_key)
                    st.rerun()
                except Exception as error:
                    st.error("Simulation stopped safely: " + str(error))
            if cancel_col.button("Cancel authorization", key=f"sb48-cancel-auth-{task.task_id}"):
                st.error("An issued durable authorization cannot be silently cancelled; it expires shortly and will never be replayed automatically. Start a new mission for another attempt.")


def _render_result(st, service, state, task_id, status):
    label = {"completed": "Completed", "failed": "Failed", "cancelled": "Cancelled",
             "timed_out": "Reconciliation required", "interrupted": "Reconciliation required",
             "running": "In progress", "authorization_issued": "Authorized; not submitted"}.get(status, status)
    st.markdown("### Simulation result")
    st.write("**Status:** " + label)
    st.info("Synthetic fixture result only — no genuine research or external action occurred.")
    if status == "completed":
        projection = service.lookup(state, task_id)
        result = projection.get("result")
        if result is not None and isinstance(result.output, dict):
            st.write("**Plain-language summary:** The local synthetic specialist produced an illustrative report for the approved objective.")
            st.json(result.output)
            assessment = state.outcomes.get(task_id)
            if assessment is not None:
                st.caption("Structural fixture assessment: " + assessment.status.value + ". This is not independent fact verification.")
    elif status in {"interrupted", "timed_out"}:
        st.warning("Outcome is uncertain. The system will not replay this simulation; review the durable execution history.")
    st.write("**Execution history:** " + status)
