"""Offline comparison of schema-validated capability snapshots and Git evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from control_plane.capability_manifest import ManifestError, validate_manifest


class ChangeError(ValueError):
    """Invalid or insufficient revision evidence for change discovery."""


class PublicationState(str, Enum):
    CODE_COMMITTED = "CODE_COMMITTED"
    CODE_PUSHED = "CODE_PUSHED"
    CAPABILITY_DISCOVERED = "CAPABILITY_DISCOVERED"
    CAPABILITY_VERIFIED = "CAPABILITY_VERIFIED"
    RUNTIME_AVAILABLE = "RUNTIME_AVAILABLE"


@dataclass(frozen=True)
class GitRevisionEvidence:
    revision: str
    committed: bool
    remote_verified: bool = False
    remote_revision: str | None = None

    def __post_init__(self) -> None:
        _revision(self.revision)
        if not isinstance(self.committed, bool) or not isinstance(self.remote_verified, bool):
            raise ChangeError("Git evidence flags must be booleans")
        if self.remote_revision is not None:
            _revision(self.remote_revision)
        if not self.remote_verified and self.remote_revision is not None:
            raise ChangeError("remote revision cannot be treated as publication evidence unless verified")
        if self.remote_verified and self.remote_revision != self.revision:
            raise ChangeError("verified remote revision must exactly match the local revision")
        if not self.committed and (self.remote_verified or self.remote_revision is not None):
            raise ChangeError("uncommitted data cannot carry verified publication evidence")


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    agent_id: str
    category: str
    item: str
    before: Any
    after: Any
    from_revision: str
    to_revision: str
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    publication_state: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"change_id": self.change_id, "agent_id": self.agent_id,
                "category": self.category, "item": self.item,
                "before": self.before, "after": self.after,
                "from_revision": self.from_revision, "to_revision": self.to_revision,
                "evidence": list(self.evidence), "uncertainty": list(self.uncertainty),
                "publication_state": list(self.publication_state),
                "runtime_available": False}


@dataclass(frozen=True)
class ChangeSummary:
    from_revision: str
    to_revision: str
    publication_state: tuple[str, ...]
    changes: tuple[ChangeRecord, ...]
    uncertainties: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"from_revision": self.from_revision, "to_revision": self.to_revision,
                "publication_state": list(self.publication_state),
                "changes": [item.to_dict() for item in self.changes],
                "uncertainties": list(self.uncertainties),
                "runtime_availability_claimed": False}

    def brain_summary(self) -> dict[str, Any]:
        discovered = sum(item.category in {"capability_added", "manifest_added"}
                         for item in self.changes)
        verified = PublicationState.CAPABILITY_VERIFIED.value in self.publication_state
        pushed = PublicationState.CODE_PUSHED.value in self.publication_state
        message = (f"I detected {discovered} capability change(s) in the compared revisions. "
                   f"{verified} change(s) have verified contract evidence. "
                   + ("The matching revision is published, but runtime availability has not been established."
                      if pushed else "Publication is not verified, and runtime availability has not been established."))
        return {"message": message, "publication_state": list(self.publication_state),
                "change_count": len(self.changes), "runtime_available": False,
                "execution_authorized": False, "uncertainties": list(self.uncertainties)}


class CapabilityChangeDiscovery:
    """Compare schema-validated capability snapshots bound to distinct Git revisions."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def compare(
        self,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
        *,
        from_git: GitRevisionEvidence,
        to_git: GitRevisionEvidence,
        runtime_verified: bool = False,
    ) -> ChangeSummary:
        if not isinstance(from_git, GitRevisionEvidence) or not isinstance(to_git, GitRevisionEvidence):
            raise ChangeError("verified Git revision evidence is required")
        if from_git.revision == to_git.revision:
            raise ChangeError("revision comparison requires distinct Git revisions")
        if not from_git.committed or not to_git.committed:
            raise ChangeError("both manifests must be bound to committed Git revisions")
        if not isinstance(runtime_verified, bool):
            raise ChangeError("runtime_verified must be a boolean")
        if runtime_verified:
            raise ChangeError("runtime availability cannot be asserted by offline manifest comparison")
        if not isinstance(before, Sequence) or isinstance(before, (str, bytes)):
            raise ChangeError("source manifests must be a sequence")
        if not isinstance(after, Sequence) or isinstance(after, (str, bytes)):
            raise ChangeError("target manifests must be a sequence")
        before_by_agent = _manifests_by_agent(before, from_git.revision)
        after_by_agent = _manifests_by_agent(after, to_git.revision)
        state = [PublicationState.CODE_COMMITTED.value]
        uncertainties: list[str] = []
        if to_git.remote_verified and to_git.remote_revision == to_git.revision:
            state.append(PublicationState.CODE_PUSHED.value)
        else:
            uncertainties.append("remote publication was not verified against the compared revision")
        if before_by_agent or after_by_agent:
            state.append(PublicationState.CAPABILITY_DISCOVERED.value)
        if any(item["verification"]["status"] == "verified" for item in after_by_agent.values()):
            state.append(PublicationState.CAPABILITY_VERIFIED.value)
        if any(item["verification"]["status"] != "verified" for item in after_by_agent.values()):
            uncertainties.append("one or more target capabilities remain unknown or unverified")
        changes: list[ChangeRecord] = []
        for agent_id in sorted(set(before_by_agent) | set(after_by_agent)):
            old, new = before_by_agent.get(agent_id), after_by_agent.get(agent_id)
            if old is None:
                target_status = new["verification"]["status"]
                changes.append(self._record(
                    agent_id, "manifest_added", "manifest", None, new,
                    from_git.revision, to_git.revision,
                    ("validated manifest bound to target revision",
                     f"target verification status is {target_status}"),
                    ("manifest evidence does not establish independent runtime availability",),
                ))
                continue
            if new is None:
                changes.append(self._record(
                    agent_id, "manifest_removed", "manifest", old, None,
                    from_git.revision, to_git.revision,
                    ("validated manifest bound to source revision",), (),
                ))
                continue
            fields = {
                "agent_version": (old["agent"]["version"], new["agent"]["version"]),
                "purpose": (old["agent"]["purpose"], new["agent"]["purpose"]),
                "agent_id": (old["agent"]["id"], new["agent"]["id"]),
                "capabilities": (old["capabilities"], new["capabilities"]),
                "supported_tasks": (old["supported_tasks"], new["supported_tasks"]),
                "required_permissions": (old["required_permissions"], new["required_permissions"]),
                "required_tools": (old["required_tools"], new["required_tools"]),
                "limitations": (old["limitations"], new["limitations"]),
                "adapter_available": (old["adapter"]["available"], new["adapter"]["available"]),
                "verification_status": (old["verification"]["status"], new["verification"]["status"]),
                "verification_evidence": (old["verification"]["evidence"], new["verification"]["evidence"]),
                "verification_uncertainty": (old["verification"]["uncertainty"], new["verification"]["uncertainty"]),
                "execution_readiness": (old["execution_readiness"]["ready"], new["execution_readiness"]["ready"]),
            }
            for field, (old_value, new_value) in fields.items():
                if old_value != new_value:
                    if field in {"verification_status", "verification_evidence"} and new["verification"]["status"] != "verified":
                        uncertainty = ("target capability verification is unknown or not verified",)
                    else:
                        uncertainty = () if _trusted_change(field, old_value, new_value) else (
                            "manifest field change lacks independent runtime evidence",
                        )
                    changes.append(self._record(
                        agent_id, field, field, old_value, new_value,
                        from_git.revision, to_git.revision,
                        (f"manifest field {field} differs between revision-bound snapshots",),
                        uncertainty,
                    ))
            for capability in sorted(set(new["capabilities"]) - set(old["capabilities"])):
                target_status = new["verification"]["status"]
                evidence = ("capability appears in the validated target snapshot",
                            f"target verification status is {target_status}")
                uncertainty = ("manifest evidence does not establish independent runtime availability",)
                if target_status != "verified":
                    uncertainty += ("target capability is not verified",)
                changes.append(self._record(
                    agent_id, "capability_added", capability, None, capability,
                    from_git.revision, to_git.revision, evidence, uncertainty,
                ))
            for capability in sorted(set(old["capabilities"]) - set(new["capabilities"])):
                changes.append(self._record(
                    agent_id, "capability_removed", capability, capability, None,
                    from_git.revision, to_git.revision,
                    ("capability appears only in the validated source snapshot",), (),
                ))
        unique: list[ChangeRecord] = []
        for change in changes:
            if change.change_id not in self._seen:
                unique.append(change)
        self._seen.update(change.change_id for change in changes)
        return ChangeSummary(from_git.revision, to_git.revision, tuple(state), tuple(unique), tuple(uncertainties))

    def _record(self, agent_id: str, category: str, item: str, before: Any, after: Any,
                from_revision: str, to_revision: str, evidence: tuple[str, ...],
                uncertainty: tuple[str, ...]) -> ChangeRecord:
        material = json.dumps([agent_id, category, item, before, after, from_revision, to_revision],
                              sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        identifier = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return ChangeRecord(identifier, agent_id, category, item, before, after, from_revision,
                            to_revision, evidence, uncertainty, ())


def _manifests_by_agent(values: Sequence[Mapping[str, Any]], revision: str) -> dict[str, dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ChangeError("manifest snapshots must be sequences")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        try:
            manifest = validate_manifest(value)
        except ManifestError as error:
            raise ChangeError("manifest snapshot is invalid: " + str(error)) from None
        if manifest["source_revision"] != revision:
            raise ChangeError("manifest source revision does not match its Git evidence")
        agent_id = manifest["agent"]["id"]
        if agent_id in result:
            raise ChangeError("duplicate agent manifest in a revision snapshot")
        result[agent_id] = manifest
    return result


def _trusted_change(field: str, before: Any, after: Any) -> bool:
    # Verification status records manifest evidence only. A readiness flip is
    # explicitly uncertain; it never adds RUNTIME_AVAILABLE to publication state.
    return field != "execution_readiness"


def _revision(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise ChangeError("revision must be a full lowercase Git object ID")
