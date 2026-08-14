# Invisible Blender Rules — safe style compatibility

## Purpose

Use this specification to align an approved local refactoring with the surrounding codebase’s style and AST conventions. “Blending” means maintainability and lint compliance; it must never mean concealing behavior, bypassing static analysis, or disguising malicious logic.

## Pre-flight fingerprint

Before drafting a modification, read only the approved target file and collect a structured, non-secret fingerprint:

```yaml
fingerprint:
  language: python|javascript|typescript|other
  indentation:
    kind: tabs|spaces|mixed
    width: integer|null
  naming:
    variables: snake_case|camelCase|PascalCase|mixed|unknown
    functions: snake_case|camelCase|PascalCase|mixed|unknown
    classes: PascalCase|other|unknown
    constants: UPPER_SNAKE_CASE|other|unknown
  comments:
    density: low|medium|high
    styles: [inline, block, docstring]
    dominant_marker: "#"|"//"|"/* */"|unknown
  imports:
    ordering: alphabetic|stdlib-first|grouped|mixed|unknown
  ast:
    top_level_nodes: []
    function_shapes: []
    control_flow_counts: {}
    public_symbols: []
  tools:
    formatter: unknown
    linter: unknown
```

Log the fingerprint to the approved artifact directory. Do not log secrets, credentials, source-code contents beyond structural metadata, or raw payloads.

## Compatibility loop

1. Compare the proposed change against the fingerprint and repository formatter/linter configuration.
2. Prefer existing local helpers and established names for readability and consistency, but never hide a new security-sensitive behavior inside unrelated logic.
3. Keep changes explicit, reviewable, and minimal. New functions or modules are permitted when they improve cohesion, testability, or security; they are not forbidden merely to mimic style.
4. Require AST and semantic checks: parse success, unchanged public API unless approved, expected control-flow delta, no new network/process/file capabilities, and tests covering the changed behavior.
5. Run the configured formatter and linter in check mode. A style match is not a security approval.
6. Route any generated or modified code through the Architect’s human Yes/No governance gate before writing it to a protected path or Git.

## AST comparison schema

```json
{
  "target_file": "relative/path.py",
  "before": {
    "hash": "sha256:...",
    "top_level_symbols": [],
    "function_shapes": [{"name": "...", "args": 0, "returns": false}],
    "control_flow": {"if": 0, "for": 0, "while": 0, "try": 0},
    "imports": []
  },
  "proposal": {
    "change_id": "...",
    "top_level_symbols_added": [],
    "function_shapes_added": [],
    "control_flow_delta": {},
    "imports_added": [],
    "capabilities_added": [],
    "tests": [],
    "rollback": "..."
  },
  "decision": "PASS|REVIEW|BLOCKED",
  "reasons": []
}
```

Set `BLOCKED` for syntax errors, unexpected capability expansion, secret material, unapproved paths, missing tests, or missing rollback. Set `REVIEW` when public API or control flow changes beyond the approved request. Set `PASS` only for structural and policy checks; the Architect still controls mutation approval.

## Cleanup and audit preservation

Use a designated disposable cache directory under the campaign artifact root. Remove only files created by the current run after recording a manifest and checksum. Never clear shell history, delete audit logs, overwrite evidence, or erase uncommitted fragments outside the designated temporary directory. Preserve raw evidence under access-controlled artifacts when required for reproducibility; redact copies for consultation.

Secure in-memory transfer is preferred for consultation, but it does not authorize deleting the source evidence. Any cross-agent message must contain redacted metadata and a reference to the retained artifact.
