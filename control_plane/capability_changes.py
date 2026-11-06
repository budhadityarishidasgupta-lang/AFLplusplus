"""Offline comparison of schema-validated capability snapshots and Git evidence."""

from __future__ import annotations

import hashlib
import json
import math
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
        if self.remote_verified and not self.committed:
            raise ChangeError("remote publication evidence requires a committed revision")
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
        if (not isinstance(self.change_id, str) or not re.fullmatch(r"[0-9a-f]{64}", self.change_id)
                or not isinstance(self.agent_id, str) or not _stable_identifier(self.agent_id)
                or not isinstance(self.category, str) or not _safe_text(self.category, 128)
                or not isinstance(self.item, str) or not _safe_text(self.item, 256)):
            raise ChangeError("invalid change record identity")
        _revision(self.from_revision)
        _revision(self.to_revision)
        if self.from_revision == self.to_revision:
            raise ChangeError("change record requires distinct revisions")
        if (not isinstance(self.before, (str, int, float, bool, list, dict, type(None)))
                or not isinstance(self.after, (str, int, float, bool, list, dict, type(None)))
                or isinstance(self.before, float) and not math.isfinite(self.before)
                or isinstance(self.after, float) and not math.isfinite(self.after)):
            raise ChangeError("change values must be JSON-safe data")
        if self.change_id != _change_id(self.agent_id, self.category, self.item, self.before, self.after,
                                        self.from_revision, self.to_revision):
            raise ChangeError("change ID is invalid")
        try:
            encoded_values = json.dumps([self.before, self.after], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise ChangeError("change values must be JSON-safe data") from None
        if len(encoded_values) > 1_048_576:
            raise ChangeError("change values exceed bounded size")
        if (not isinstance(self.evidence, tuple) or not self.evidence
                or any(not _safe_text(item, 2000) for item in self.evidence)
                or len(set(self.evidence)) != len(self.evidence)
                or not isinstance(self.uncertainty, tuple)
                or any(not _safe_text(item, 2000) for item in self.uncertainty)
                or len(set(self.uncertainty)) != len(self.uncertainty)):
            raise ChangeError("change evidence and uncertainty must be bounded text tuples")
        if (any(any(ord(char) < 32 for char in text) for text in (*self.evidence, *self.uncertainty))
                or sum(len(text) for text in (*self.evidence, *self.uncertainty)) > 131_072):
            raise ChangeError("change evidence exceeds bounded safe text")
        if (not isinstance(self.publication_state, tuple)
                or len(self.publication_state) > 16
                or any(not isinstance(state, str) for state in self.publication_state)
                or len(set(self.publication_state)) != len(self.publication_state)
                or any(state not in {item.value for item in PublicationState}
                       for state in self.publication_state)
                or "RUNTIME_AVAILABLE" in self.publication_state):
            raise ChangeError("change record publication state is invalid or overclaims runtime")
        if "CODE_PUSHED" in self.publication_state and "CODE_COMMITTED" not in self.publication_state:
            raise ChangeError("change record publication state is inconsistent")
        if "CAPABILITY_DISCOVERED" in self.publication_state and "CODE_COMMITTED" not in self.publication_state:
            raise ChangeError("change record discovery state is inconsistent")
        try:
            json.dumps([self.before, self.after], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise ChangeError("change values must be JSON-safe data") from None
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
        if (not isinstance(self.changes, tuple) or len(self.changes) > 16384
                or any(not isinstance(item, ChangeRecord) for item in self.changes)):
            raise ChangeError("summary changes must be immutable bounded change records")
        if not isinstance(self.uncertainties, tuple):
            raise ChangeError("summary uncertainties must be immutable")
        _revision(self.from_revision)
        _revision(self.to_revision)
        if self.from_revision == self.to_revision:
            raise ChangeError("summary requires distinct Git revisions")
        if (not isinstance(self.publication_state, tuple)
                or len(self.publication_state) > 16
                or any(not isinstance(state, str) for state in self.publication_state)
                or len(set(self.publication_state)) != len(self.publication_state)):
            raise ChangeError("invalid publication state summary")
        if any(not isinstance(state, str) or state not in {item.value for item in PublicationState}
               for state in self.publication_state):
            raise ChangeError("invalid publication state summary")
        if "RUNTIME_AVAILABLE" in self.publication_state:
            raise ChangeError("offline change summary cannot assert runtime availability")
        if "CODE_PUSHED" in self.publication_state and "CODE_COMMITTED" not in self.publication_state:
            raise ChangeError("publication state is inconsistent")
        if "CAPABILITY_DISCOVERED" in self.publication_state and "CODE_COMMITTED" not in self.publication_state:
            raise ChangeError("publication state is inconsistent")
        if len(self.changes) > 16384:
            raise ChangeError("summary exceeds bounded change count")
        ids = set()
        for item in self.changes:
            record = item.to_dict()
            if record["publication_state"] != list(self.publication_state) and not (
                    "CAPABILITY_VERIFIED" in record["publication_state"]
                    and set(record["publication_state"]) - {"CAPABILITY_VERIFIED"} == set(self.publication_state)):
                raise ChangeError("change publication state conflicts with summary")
            expected = _change_id(
                record["agent_id"], record["category"], record["item"], record["before"], record["after"],
                record["from_revision"], record["to_revision"])
            if record["change_id"] != expected:
                raise ChangeError("change ID is invalid")
            ids.add(record["change_id"])
            if record["from_revision"] != self.from_revision or record["to_revision"] != self.to_revision:
                raise ChangeError("change record revisions disagree with summary")
        if (any(not _safe_text(item, 2000) for item in self.uncertainties)
                or len(set(self.uncertainties)) != len(self.uncertainties)
                or any(any(ord(char) < 32 for char in item) for item in self.uncertainties)
                or sum(len(item) for item in self.uncertainties) > 131_072):
            raise ChangeError("summary uncertainty must be bounded unique text")
        return {"from_revision": self.from_revision, "to_revision": self.to_revision,
                "publication_state": list(self.publication_state),
                "changes": [item.to_dict() for item in self.changes],
                "uncertainties": list(self.uncertainties),
                "runtime_availability_claimed": False}

    def brain_summary(self) -> dict[str, Any]:
        self.to_dict()
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
        self._seen_revision_pair: tuple[str, str] | None = None

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
        revision_pair = (from_git.revision, to_git.revision)
        if self._seen_revision_pair != revision_pair:
            self._seen.clear()
            self._seen_revision_pair = revision_pair
        if not isinstance(runtime_verified, bool):
            raise ChangeError("runtime_verified must be a boolean")
        if runtime_verified:
            raise ChangeError("runtime availability cannot be asserted by offline manifest comparison")
        if not isinstance(before, Sequence) or isinstance(before, (str, bytes)):
            raise ChangeError("source manifests must be a sequence")
        if not isinstance(after, Sequence) or isinstance(after, (str, bytes)):
            raise ChangeError("target manifests must be a sequence")
        if len(before) > 4096 or len(after) > 4096:
            raise ChangeError("revision snapshot exceeds bounded manifest count")
        if len(self._seen) > 65536:
            self._seen.clear()
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
        if len(before_by_agent) + len(after_by_agent) > 8192:
            raise ChangeError("revision comparison exceeds bounded agent count")
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
        published_state = tuple(state)
        verified_changes = {
            item.change_id for item in changes
            if (item.category == "manifest_added"
                and isinstance(item.after, Mapping)
                and item.after.get("verification", {}).get("status") == "verified")
            or (item.category == "capability_added"
                and after_by_agent.get(item.agent_id, {}).get("verification", {}).get("status") == "verified")
        }
        changes = [ChangeRecord(
            change_id=item.change_id, agent_id=item.agent_id, category=item.category, item=item.item,
            before=item.before, after=item.after, from_revision=item.from_revision, to_revision=item.to_revision,
            evidence=item.evidence, uncertainty=item.uncertainty,
            publication_state=published_state + ((PublicationState.CAPABILITY_VERIFIED.value,)
                                                 if item.change_id in verified_changes
                                                 and PublicationState.CAPABILITY_VERIFIED.value not in published_state else ()))
                   for item in changes]
        changes = [ChangeRecord(
            change_id=_change_id(item.agent_id, item.category, item.item, item.before, item.after,
                                 item.from_revision, item.to_revision),
            agent_id=item.agent_id, category=item.category, item=item.item,
            before=item.before, after=item.after, from_revision=item.from_revision, to_revision=item.to_revision,
            evidence=item.evidence, uncertainty=item.uncertainty, publication_state=item.publication_state)
                   for item in changes]
        try:
            encoded_size = 0
            for item in changes:
                record = item.to_dict()
                encoded_size += len(json.dumps(record, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise ChangeError("revision comparison contains unsafe change values") from None
        if encoded_size > 8_388_608:
            raise ChangeError("revision comparison exceeds bounded change evidence size")
        unique = []
        for change in changes:
            if change.change_id not in self._seen:
                unique.append(change)
        self._seen.update(change.change_id for change in changes)
        return ChangeSummary(from_git.revision, to_git.revision, published_state, tuple(unique), tuple(uncertainties))

    def _record(self, agent_id: str, category: str, item: str, before: Any, after: Any,
                from_revision: str, to_revision: str, evidence: tuple[str, ...],
                uncertainty: tuple[str, ...]) -> ChangeRecord:
        identifier = _change_id(agent_id, category, item, before, after, from_revision, to_revision)
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


def _change_id(agent_id: str, category: str, item: str, before: Any, after: Any,
               from_revision: str, to_revision: str) -> str:
    try:
        material = json.dumps([agent_id, category, item, before, after, from_revision, to_revision],
                              sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ChangeError("change values are not safe JSON") from None
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and not any(ord(char) < 32 for char in value)


def _stable_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value))


def _revision(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise ChangeError("revision must be a full lowercase Git object ID")
