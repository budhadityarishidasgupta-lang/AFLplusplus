"""Ordinary Studio rendering for persisted mission review and status.

Synthetic acceptance execution is deliberately absent from the production UI.
Only the trusted automated acceptance harness injects that service directly.
"""
from __future__ import annotations


def _load_state(st, conversation, mission_conversation, mission_controller, session_id, data_dir, db_path):
    repository = mission_controller.ConversationMissionRepository(
        conversation, data_dir=data_dir, db_path=db_path)
    repository.synthetic_fixture_discovery = None
    loaded = repository.load(session_id)
    if loaded is None:
        return repository, None, None
    state, request_key = loaded
    return repository, state, request_key


def render(st, conversation, mission_conversation, mission_controller, mission_execution,
           session_id, data_dir, db_path):
    """Render plans, planning approvals, and fail-closed execution status only."""
    repository, state, request_key = _load_state(
        st, conversation, mission_conversation, mission_controller, session_id, data_dir, db_path)
    if state is None:
        return
    state.synthetic_acceptance = False
    st.subheader("Mission plan and execution review")
    st.info(mission_conversation.describe_state(state))
    for task in state.plan.tasks:
        with st.expander(f"Task: {task.task_id}", expanded=True):
            st.write(task.description)
            st.write(f"Proposed agent: {task.proposed_agent or 'Unavailable'}")
            st.write(f"Capability: {', '.join(task.required_capabilities) or 'none declared'}")
            st.write(f"Required permissions: {', '.join(task.required_permissions) or 'none declared'}")
            if task.missing_prerequisites:
                st.warning("Unavailable or missing: " + "; ".join(task.missing_prerequisites))
            planning = state.approvals.get(task.task_id)
            decision = planning[1] if planning else None
            if planning is None and task.proposed_agent is not None:
                if st.button("Review approval request", key=f"mission_approval_review_{task.task_id}"):
                    try:
                        mission_conversation.create_approval_request(state, task.task_id)
                        if not repository.persist(session_id, state, request_key):
                            raise mission_conversation.MissionConversationError("Planning request was not persisted.")
                        st.rerun()
                    except (mission_conversation.MissionConversationError, ValueError, OSError) as error:
                        st.error(str(error))
            if planning is not None and decision is None:
                st.subheader("Approval request details")
                st.write({"Mission": planning[0].mission_ref, "Task": planning[0].task_ref,
                          "Proposed agent": planning[0].proposed_agent,
                          "Capability": planning[0].capability,
                          "Requested permissions": planning[0].requested_permissions,
                          "Expected outcome": planning[0].expected_outcome})
                approve_col, reject_col = st.columns(2)
                if approve_col.button("Approve this task", key=f"mission_approve_{task.task_id}"):
                    try:
                        mission_conversation.decide_approval(
                            state, task.task_id, approved=True, human_identity="chat-user",
                            reason="Explicit user approval from mission review UI")
                        if not repository.persist(session_id, state, request_key):
                            raise mission_conversation.MissionConversationError("Planning approval was not persisted.")
                        st.rerun()
                    except (mission_conversation.MissionConversationError, ValueError, OSError) as error:
                        st.error(str(error))
                if reject_col.button("Reject this task", key=f"mission_reject_{task.task_id}"):
                    try:
                        mission_conversation.decide_approval(
                            state, task.task_id, approved=False, human_identity="chat-user",
                            reason="Explicit user rejection from mission review UI")
                        if not repository.persist(session_id, state, request_key):
                            raise mission_conversation.MissionConversationError("Planning decision was not persisted.")
                        st.rerun()
                    except (mission_conversation.MissionConversationError, ValueError, OSError) as error:
                        st.error(str(error))
            elif planning is not None:
                st.caption("Planning approval status: " + (decision.status.value if decision else "Pending"))
            record = (state.execution_records or {}).get(task.task_id, {})
            if record:
                st.caption("Execution state: " + str(record.get("status", "unknown")))
                st.info("Execution authorization and synthetic submission are unavailable in ordinary Studio sessions.")
            else:
                st.caption("Execution state: not authorized; no durable claim")
                st.info("Planning approval does not authorize execution. No specialist or external action is available.")
            st.info("Synthetic acceptance capability is unavailable in ordinary Studio sessions.")


def route_explicit_execution_request(text: str) -> bool:
    """Recognize intent without granting or triggering execution authority."""
    import re
    return bool(re.search(r"\b(?:request|approve|authorize|confirm)\b.*\bsynthetic\s+execution\b", text, re.I))
