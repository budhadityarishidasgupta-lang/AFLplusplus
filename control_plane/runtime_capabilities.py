"""Read-only reconciliation of published capability metadata and local runtime evidence.

This module never loads or invokes specialist agents. Runtime observations must be
constructed from locally established evidence; even VERIFIED_AVAILABLE grants no
execution permission or authorization.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from control_plane.capability_manifest import ManifestError, validate_manifest


class RuntimeCapabilityError(ValueError):
    """Invalid, conflicting, or insufficient runtime capability data."""


class RuntimeState(str, Enum):
    NOT_DEPLOYED = "NOT_DEPLOYED"
    INSTALLED = "INSTALLED"
    LOADED = "LOADED"
    VERIFIED_AVAILABLE = "VERIFIED_AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


_REVISION = re.compile(r"[0-9a-f]{40,64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class RuntimeEvidence:
    """Locally established observation for one installed adapter and revision."""

    agent_id: str
    capability: str
    installed_revision: str
    software_revision: str
    adapter_id: str
    adapter_registered: bool
    interface_compatible: bool
    dependencies: tuple[str, ...]
    missing_dependencies: tuple[str, ...]
    execution_integration_verified: bool
    permissions: tuple[str, ...]
    health_check_passed: bool
    version_compatible: bool
    unavailable: bool = False
    locally_established: bool = True

    def __post_init__(self) -> None:
        for label, value in (("agent_id", self.agent_id), ("capability", self.capability),
                             ("adapter_id", self.adapter_id)):
            if not isinstance(value, str) or len(value) > 128 or not _IDENTIFIER.fullmatch(value):
                raise RuntimeCapabilityError(f"{label} must be a stable identifier")
        for label, value in (("installed_revision", self.installed_revision),
                             ("software_revision", self.software_revision)):
            if not isinstance(value, str) or not _REVISION.fullmatch(value):
                raise RuntimeCapabilityError(f"{label} must be a full lowercase Git revision")
        for name in ("adapter_registered", "interface_compatible", "execution_integration_verified",
                     "health_check_passed", "version_compatible", "unavailable", "locally_established"):
            if not isinstance(getattr(self, name), bool):
                raise RuntimeCapabilityError(f"{name} must be boolean")
        if not self.locally_established:
            raise RuntimeCapabilityError("runtime evidence must be locally established")
        if not self.adapter_registered and any((
            self.interface_compatible,
            self.execution_integration_verified,
            self.health_check_passed,
            self.version_compatible,
        )):
            raise RuntimeCapabilityError("unregistered adapter cannot claim loaded runtime verification")
        for label, values in (("dependencies", self.dependencies),
                              ("missing_dependencies", self.missing_dependencies),
                              ("permissions", self.permissions)):
            _text_tuple(values, label)
        if set(self.missing_dependencies) - set(self.dependencies):
            raise RuntimeCapabilityError("missing dependencies must be declared dependencies")


@dataclass(frozen=True)
class CapabilityRuntimeAssessment:
    agent_id: str
    capability: str
    state: RuntimeState
    published: bool
    installed: bool
    loaded: bool
    verified: bool
    execution_eligible: bool
    required_permissions: tuple[str, ...]
    runtime_permissions: tuple[str, ...]
    reasons: tuple[str, ...]
    verification_required: tuple[str, ...]
    published_revision: str | None
    installed_revision: str | None
    software_revision: str | None
    execution_authorized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capability": self.capability,
            "state": self.state.value,
            "published": self.published,
            "installed": self.installed,
            "loaded": self.loaded,
            "verified": self.verified,
            "execution_eligible": self.execution_eligible,
            "required_permissions": list(self.required_permissions),
            "runtime_permissions": list(self.runtime_permissions),
            "reasons": list(self.reasons),
            "verification_required": list(self.verification_required),
            "published_revision": self.published_revision,
            "installed_revision": self.installed_revision,
            "software_revision": self.software_revision,
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class RuntimeCapabilityReport:
    assessments: tuple[CapabilityRuntimeAssessment, ...]
    brain_summary_text: str
    execution_authorized: bool = False

    def to_dict(self) -> dict[str, Any]:
        report = {
            "assessments": [assessment.to_dict() for assessment in self.assessments],
            "summary": self.brain_summary_text,
            "execution_authorized": False,
            "runtime_available_claimed": any(
                assessment.state is RuntimeState.VERIFIED_AVAILABLE for assessment in self.assessments
            ),
        }
        try:
            json.dumps(report, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise RuntimeCapabilityError("runtime report is not JSON serializable") from None
        return report

    def brain_summary(self) -> dict[str, Any]:
        return {"message": self.brain_summary_text,
                "execution_authorized": False,
                "capability_count": len(self.assessments),
                "verified_available_count": sum(
                    item.state is RuntimeState.VERIFIED_AVAILABLE for item in self.assessments
                )}


class RuntimeCapabilityVerifier:
    """Assess local installation/adapter evidence without importing or executing agents."""

    def verify(
        self,
        published_manifests: Sequence[Mapping[str, Any]],
        installed_manifests: Sequence[Mapping[str, Any]],
        runtime_evidence: Sequence[RuntimeEvidence],
        *,
        running_revision: str,
    ) -> RuntimeCapabilityReport:
        if not isinstance(runtime_evidence, Sequence) or isinstance(runtime_evidence, (str, bytes)):
            raise RuntimeCapabilityError("runtime evidence must be a sequence")
        _revision(running_revision, "running_revision")
        published = _validated_manifests(published_manifests, "published")
        installed = _validated_manifests(installed_manifests, "installed")
        evidence_by_capability: dict[tuple[str, str], RuntimeEvidence] = {}
        for evidence in runtime_evidence:
            if not isinstance(evidence, RuntimeEvidence):
                raise RuntimeCapabilityError("runtime evidence must use the local RuntimeEvidence contract")
            key = (evidence.agent_id, evidence.capability)
            if key in evidence_by_capability:
                raise RuntimeCapabilityError("conflicting runtime evidence for capability")
            evidence_by_capability[key] = evidence
        if len(published) and any(manifest["source_revision"] is None for manifest in published.values()):
            raise RuntimeCapabilityError("published manifest is missing its revision")
        if len(installed) and any(manifest["source_revision"] is None for manifest in installed.values()):
            raise RuntimeCapabilityError("installed manifest is missing its revision")
        published_pairs = _capabilities(published)
        installed_pairs = _capabilities(installed)
        for agent_id in set(installed) - set(published):
            for capability in installed[agent_id]["capabilities"]:
                if (agent_id, capability) in evidence_by_capability:
                    raise RuntimeCapabilityError("runtime evidence conflicts with absent published manifest")
        for agent_id in set(published) & set(installed):
            pub = published[agent_id]
            local = installed[agent_id]
            if (pub["agent"]["version"] == local["agent"]["version"]
                    and pub["source_revision"] == local["source_revision"]
                    and (pub["agent"] != local["agent"]
                         or pub["capabilities"] != local["capabilities"]
                         or pub["required_permissions"] != local["required_permissions"]
                         or pub["required_tools"] != local["required_tools"]
                         or pub["supported_tasks"] != local["supported_tasks"]
                         or (pub["verification"]["status"] == "verified"
                             and local["verification"]["status"] == "verified"
                             and pub["verification"] != local["verification"])
                         or pub["adapter"] != local["adapter"]
                         or pub["execution_readiness"] != local["execution_readiness"])):
                raise RuntimeCapabilityError("published and installed manifests conflict at the same revision")
        for key, evidence in evidence_by_capability.items():
            if key not in installed_pairs:
                raise RuntimeCapabilityError("runtime evidence conflicts with the installed manifest")
            if evidence.software_revision != running_revision:
                raise RuntimeCapabilityError("runtime evidence conflicts with the running software revision")
        assessments: list[CapabilityRuntimeAssessment] = []
        all_pairs = sorted(published_pairs | installed_pairs | set(evidence_by_capability))
        for agent_id, capability in all_pairs:
            published_manifest = published.get(agent_id)
            installed_manifest = installed.get(agent_id)
            pub_declared = published_manifest is not None and capability in published_manifest["capabilities"]
            local_declared = installed_manifest is not None and capability in installed_manifest["capabilities"]
            requirements = tuple(published_manifest["required_permissions"]) if pub_declared else (
                tuple(installed_manifest["required_permissions"]) if local_declared else ()
            )
            evidence = evidence_by_capability.get((agent_id, capability))
            reasons: list[str] = []
            needed: list[str] = []
            installed_ok = local_declared
            loaded = False
            verified = False
            eligible = False
            installed_revision = installed_manifest["source_revision"] if installed_manifest else None
            published_revision = published_manifest["source_revision"] if published_manifest else None
            software_revision: str | None = None
            runtime_permissions: tuple[str, ...] = ()
            if not pub_declared and not local_declared:
                reasons.append("capability evidence is not present in either validated manifest")
                state = RuntimeState.UNKNOWN
            elif not pub_declared:
                reasons.append("capability is installed locally but absent from the published manifest")
                state = RuntimeState.UNKNOWN
            elif not local_declared:
                reasons.append("published capability is not installed in this runtime")
                needed.append("install the matching manifest and adapter through an approved deployment process")
                state = RuntimeState.NOT_DEPLOYED
            elif published_manifest["agent"]["version"] != installed_manifest["agent"]["version"]:
                reasons.append("installed agent version does not match the published manifest")
                needed.append("reconcile the installed agent version with the published version")
                state = RuntimeState.UNAVAILABLE
            elif evidence is not None and evidence.unavailable:
                reasons.append("runtime evidence marks the capability unavailable")
                state = RuntimeState.UNAVAILABLE
            elif evidence is not None and evidence.missing_dependencies:
                reasons.extend(f"required dependency is missing: {name}"
                               for name in evidence.missing_dependencies)
                needed.append("install required dependencies through the approved deployment process")
                state = RuntimeState.UNAVAILABLE
            elif published_manifest["verification"]["status"] != "verified":
                reasons.append("published contract metadata is not statically verified")
                needed.append("verify the published capability contract and trusted source evidence")
                state = RuntimeState.UNKNOWN
            elif installed_manifest["verification"]["status"] != "verified":
                reasons.append("installed contract metadata is not statically verified")
                needed.append("verify the installed capability contract and trusted source evidence")
                state = RuntimeState.UNKNOWN
            elif installed_manifest["source_revision"] != published_manifest["source_revision"]:
                reasons.append("installed manifest revision differs from the published revision")
                needed.append("install and verify the published revision")
                state = RuntimeState.UNAVAILABLE
            elif evidence is not None and evidence.installed_revision != installed_manifest["source_revision"]:
                reasons.append("runtime evidence revision differs from the installed manifest")
                state = RuntimeState.UNAVAILABLE
            elif evidence is None:
                reasons.append("installed metadata does not establish adapter loading or runtime health")
                needed.extend(("locally verify adapter registration and interface compatibility",
                               "verify dependencies, execution integration, permissions, and runtime health"))
                state = RuntimeState.INSTALLED
            else:
                software_revision = evidence.software_revision
                runtime_permissions = evidence.permissions
                mismatch = evidence.software_revision != running_revision
                missing = evidence.missing_dependencies
                if evidence.unavailable:
                    reasons.append("runtime evidence marks the capability unavailable")
                    state = RuntimeState.UNAVAILABLE
                elif mismatch:
                    reasons.append("runtime evidence software revision differs from the running revision")
                    state = RuntimeState.UNAVAILABLE
                elif missing:
                    reasons.extend(f"required dependency is missing: {name}" for name in missing)
                    needed.append("install required dependencies through the approved deployment process")
                    state = RuntimeState.UNAVAILABLE
                elif not evidence.adapter_registered:
                    reasons.append("adapter is not registered in the active runtime registry")
                    needed.append("register the adapter through the approved runtime configuration")
                    state = RuntimeState.INSTALLED
                else:
                    loaded = True
                    failures = []
                    if not evidence.interface_compatible:
                        failures.append("adapter interface compatibility is not verified")
                        needed.append("verify adapter interface compatibility")
                    if not evidence.execution_integration_verified:
                        failures.append("execution integration is not independently verified")
                        needed.append("verify the execution integration using trusted local evidence")
                    if not evidence.health_check_passed:
                        failures.append("runtime health evidence is missing or failed")
                        needed.append("obtain a passing local runtime health check")
                    if not evidence.version_compatible:
                        failures.append("runtime version compatibility is not verified")
                        needed.append("verify adapter and agent version compatibility")
                    if set(evidence.permissions) != set(requirements):
                        failures.append("runtime permission restrictions do not exactly match the declared contract")
                        needed.append("reconcile runtime permissions with the existing contract and approval gates")
                    if failures:
                        reasons.extend(failures)
                        state = RuntimeState.LOADED
                    else:
                        state = RuntimeState.VERIFIED_AVAILABLE
                        verified = True
                        eligible = True
            assessments.append(CapabilityRuntimeAssessment(
                agent_id=agent_id, capability=capability, state=state,
                published=pub_declared, installed=installed_ok, loaded=loaded, verified=verified,
                execution_eligible=eligible, required_permissions=requirements,
                runtime_permissions=runtime_permissions, reasons=tuple(reasons),
                verification_required=tuple(dict.fromkeys(needed)), published_revision=published_revision,
                installed_revision=installed_revision, software_revision=software_revision,
            ))
        text = _brain_summary(assessments)
        report = RuntimeCapabilityReport(tuple(assessments), text)
        report.to_dict()
        return report


def _validated_manifests(values: Sequence[Mapping[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise RuntimeCapabilityError(f"{label} manifests must be a sequence")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        try:
            manifest = validate_manifest(value)
        except (ManifestError, TypeError) as error:
            raise RuntimeCapabilityError(f"invalid {label} manifest: {error}") from None
        agent_id = manifest["agent"]["id"]
        if agent_id in result:
            raise RuntimeCapabilityError(f"conflicting {label} manifests for the same agent")
        result[agent_id] = manifest
    return result


def _capabilities(manifests: Mapping[str, Mapping[str, Any]]) -> set[tuple[str, str]]:
    return {(agent_id, capability)
            for agent_id, manifest in manifests.items()
            for capability in manifest["capabilities"]}


def _brain_summary(assessments: Sequence[CapabilityRuntimeAssessment]) -> str:
    count = len(assessments)
    verified = sum(item.state is RuntimeState.VERIFIED_AVAILABLE for item in assessments)
    installed = sum(item.installed for item in assessments)
    published_only = sum(item.state is RuntimeState.NOT_DEPLOYED for item in assessments)
    if count == 0:
        return "I do not have a reconciled capability inventory. Runtime verification is required before any capability can be considered."
    if verified:
        return (f"I understand {count} declared capability entries. {installed} are installed, and "
                f"{verified} have locally verified runtime adapters. Runtime verification does not authorize execution.")
    if published_only:
        return (f"I understand {count} declared capability entries. {published_only} published capabilities are not installed, "
                "so I cannot command those capabilities yet. Their adapters and runtime health still require local verification.")
    return (f"I understand {count} declared capability entries. Their runtime availability is not verified, "
            "so I cannot claim that they can be commanded. Runtime verification does not authorize execution.")


def _revision(value: str, label: str) -> None:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise RuntimeCapabilityError(f"{label} must be a full lowercase Git revision")


def _text_tuple(values: tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values
    ):
        raise RuntimeCapabilityError(f"{label} must be a tuple of bounded non-empty strings")
    if len(set(values)) != len(values):
        raise RuntimeCapabilityError(f"{label} must not contain duplicates")
    if any(any(ord(char) < 32 for char in value) for value in values):
        raise RuntimeCapabilityError(f"{label} contains unsafe text")
