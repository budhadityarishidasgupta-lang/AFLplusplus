"""Secret-safe local preflight and tracked-source pattern scanner.

The scanner reads tracked files locally and only emits filename, category, and count.
Pattern matching is heuristic: novel encodings, formats, and split secrets may evade it.
"""
from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REDACTION = "[REDACTED]"


def safe_error(_exc: BaseException) -> str:
    return "[redacted error]"


def redact(text: str, secrets: tuple[str, ...] | list[str]) -> str:
    result = str(text)
    for secret in secrets:
        if secret:
            for variant in (secret, secret.encode().hex(), base64.b64encode(secret.encode()).decode()):
                if variant:
                    result = result.replace(variant, REDACTION)
    result = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", rf"\1{REDACTION}", result)
    result = re.sub(r"(?i)(\b(?:api[_-]?key|token|secret)\b\s*[:=]\s*)[^\s,;]+", rf"\1{REDACTION}", result)
    return result


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)


def _scan_content(paths: list[tuple[str, str]]) -> list[tuple[str, str, int]]:
    """Scan in-memory path/content pairs; findings contain metadata only."""
    rules = (
        ("openai_key", re.compile(r"\bsk-(?:proj|svcacct|live|test)-[A-Za-z0-9_-]{24,}\b")),
        ("alibaba_credential", re.compile(r"\bsk-ws-[A-Za-z0-9._-]{24,}\b")),
        ("openai_assignment", re.compile(r"(?i)OPENAI_API_KEY\s*=\s*['\"]?(?!sk-(?:proj|svcacct|live|test)-)[A-Za-z0-9._~+/=-]{16,}")),
        ("alibaba_assignment", re.compile(r"(?i)DASHSCOPE_API_KEY\s*=\s*['\"]?(?!sk-ws-)[A-Za-z0-9._~+/=-]{16,}")),
        ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")),
        ("credential_assignment", re.compile(r"(?i)(?:SEARXNG_API_TOKEN|BRAVE_API_KEY|ACCESS_TOKEN)\s*=\s*['\"]?[A-Za-z0-9._~+/=-]{24,}")),
        ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    )
    findings: list[tuple[str, str, int]] = []
    for path, content in paths:
        categories: dict[str, int] = {}
        if Path(path).name == ".env" or Path(path).name.startswith(".env."):
            categories["tracked_env_file"] = 1
        for category, pattern in rules:
            count = sum(1 for _ in pattern.finditer(content))
            if count:
                categories[category] = count
        findings.extend((Path(path).as_posix(), category, count) for category, count in categories.items())
    return findings


def scan_tracked_files(root: Path = ROOT, *, git_runner=None) -> tuple[bool, list[tuple[str, str, int]]]:
    """Inspect tracked working-tree files; fail closed on inventory or read errors."""
    runner = git_runner or (lambda *args: subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    ))
    try:
        inventory = runner("ls-files", "-z")
        if inventory.returncode or not isinstance(inventory.stdout, str):
            return False, []
        if inventory.stdout and not inventory.stdout.endswith("\0"):
            return False, []
        if inventory.stdout:
            inventory_paths = inventory.stdout.split("\0")[:-1]
            if len(inventory_paths) != len(set(inventory_paths)):
                return False, []
            if any(Path(item).is_absolute() or ".." in Path(item).parts for item in inventory_paths):
                return False, []
        candidates: set[str] = set()
        status_fields: list[str] = []
        root_relative = root.resolve() != ROOT.resolve()
        if not root_relative:
            status = runner("status", "--porcelain=v1", "-z", "--untracked-files=all")
            if status.returncode or not isinstance(status.stdout, str):
                return False, []
            if status.stdout and not status.stdout.endswith("\0"):
                return False, []
            status_fields = status.stdout.split("\0")
        if status_fields and status_fields[-1] == "":
            status_fields.pop()
        index = 0
        while index < len(status_fields):
            record = status_fields[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                return False, []
            relative = record[3:]
            if root_relative:
                relative = relative.removeprefix("security-company-agent/")
            else:
                relative = ""
            if relative:
                candidates.add(relative)
            if relative and (Path(relative).is_absolute() or ".." in Path(relative).parts):
                return False, []
            if "R" in record[:2] or "C" in record[:2]:
                if index >= len(status_fields) or not status_fields[index]:
                    return False, []
                rename_path = status_fields[index]
                if root_relative:
                    rename_path = rename_path.removeprefix("security-company-agent/")
                if rename_path:
                    candidates.add(rename_path)
                if Path(rename_path).is_absolute() or ".." in Path(rename_path).parts:
                    return False, []
                index += 1
        content_pairs: list[tuple[str, str]] = []
        allowed = {".py", ".yml", ".yaml", ".json", ".ini", ".cfg", ".md", ".sh", ".txt", ".env", ".pem", ".key"}
        tracked_paths = [item for item in inventory.stdout.split("\0") if item]
        seen = set(tracked_paths)
        for relative in candidates:
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                return False, []
        candidate_paths = sorted(path for path in candidates if path not in seen)
        for relative in tracked_paths + candidate_paths:
            candidate = root / relative
            if relative in candidates and not candidate.exists():
                return False, []
            if candidate.is_symlink():
                return False, []
            if candidate.exists() and not candidate.is_file():
                return False, []
            if candidate.name == ".env" or candidate.name.startswith(".env.") or candidate.suffix.lower() in allowed:
                content_pairs.append((relative, candidate.read_text(encoding="utf-8", errors="ignore")))
        return True, _scan_content(content_pairs)
    except Exception:
        return False, []


def _changed_paths(base_ref: str = "origin/stable") -> tuple[bool, set[str], set[str]]:
    """Find PR-delta and candidate paths relative to the selected merge base."""
    try:
        merge_base = _git("merge-base", base_ref, "HEAD")
        if merge_base.returncode or not merge_base.stdout.strip():
            return False, set(), set()
        base = merge_base.stdout.strip()
        result = _git("diff", "--name-only", "-z", base, "HEAD")
        if result.returncode or not isinstance(result.stdout, str):
            return False, set(), set()
        if result.stdout and not result.stdout.endswith("\0"):
            return False, set(), set()
        pr_paths = {item for item in result.stdout.split("\0") if item}
        pr_delta_paths = set(pr_paths)
        if ROOT.resolve() != Path(__file__).resolve().parents[1]:
            return True, pr_paths, pr_delta_paths
        staged = _git("diff", "--cached", "--name-only", "-z")
        unstaged = _git("diff", "--name-only", "-z")
        if any(item.returncode or not isinstance(item.stdout, str) for item in (staged, unstaged)):
            return False, set(), set()
        for item in (staged, unstaged):
            if item.stdout and not item.stdout.endswith("\0"):
                return False, set(), set()
            pr_paths.update(path for path in item.stdout.split("\0") if path)
        status = _git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        if status.returncode or not isinstance(status.stdout, str):
            return False, set(), set()
        if status.stdout and not status.stdout.endswith("\0"):
            return False, set(), set()
        fields = status.stdout.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                return False, set(), set()
            path = record[3:]
            if path not in pr_paths and (record[:2] != "  " or "?" in record[:2]):
                pr_paths.add(path)
            if "R" in record[:2] or "C" in record[:2]:
                if index >= len(fields) or not fields[index]:
                    return False, set(), set()
                index += 1
        return True, pr_paths, pr_delta_paths
    except Exception:
        return False, set(), set()


def run_checks() -> list[tuple[str, bool | str]]:
    """Read only allowlisted local search settings; never emit their values."""
    values: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            record = line.strip()
            if not record or record.startswith("#") or "=" not in record:
                continue
            key, value = record.split("=", 1)
            if key in {"SEARXNG_ENDPOINT", "SEARXNG_API_TOKEN", "SEARXNG_APPROVED"}:
                values[key] = value.strip().strip("'\"")
        env_read = True
    except Exception:
        env_read = False

    try:
        ignored_result = _git("check-ignore", "-q", ".env")
        tracked_result = _git("ls-files", "--error-unmatch", ".env")
        git_status_ok = ignored_result.returncode in (0, 1) and tracked_result.returncode in (0, 1)
    except Exception:
        ignored_result = tracked_result = None
        git_status_ok = False
    try:
        scan_ok, findings = scan_tracked_files()
    except Exception:
        scan_ok, findings = False, []
    changed_ok, changed_paths, pr_delta_paths = _changed_paths()
    normalized_changed_paths = {Path(path).as_posix() for path in changed_paths}
    normalized_delta_paths = {Path(path).as_posix() for path in pr_delta_paths}
    new_findings = [finding for finding in findings if finding[0] in normalized_changed_paths]
    delta_findings = [finding for finding in findings if finding[0] in normalized_delta_paths]
    baseline_findings = [finding for finding in findings if finding[0] not in normalized_changed_paths]
    print(f"PR_DELTA_SECRET_FINDINGS={len(delta_findings)}")
    for path, category, count in findings:
        print(f"SECRET_FINDING file={Path(path).name} category={category} count={count}")
    print(f"SECRET_SCAN_SUMMARY new_findings={len(new_findings)} baseline_findings={len(baseline_findings)}")
    if not env_read:
        print("OWNER_CONFIG_READINESS: BLOCKED (local .env unavailable; offline validation only)")
    return [
        ("SCANNER_EXECUTION", scan_ok and changed_ok),
        ("OWNER_CONFIG_READINESS", "BLOCKED" if not env_read or not values.get("SEARXNG_API_TOKEN") else "PASS"),
        ("OFFLINE_SECURITY_VALIDATION", scan_ok and changed_ok and not new_findings and git_status_ok and ignored_result is not None and ignored_result.returncode == 0 and tracked_result is not None and tracked_result.returncode == 1 and not any(category == "tracked_env_file" for _, category, _ in findings)),
        ("LIVE_UAT", "BLOCKED"),
        ("OWNER_CONFIG_ENDPOINT_PRESENT", "BLOCKED" if not env_read or not values.get("SEARXNG_ENDPOINT") else "PASS"),
        ("OWNER_CONFIG_TOKEN_PRESENT", "BLOCKED" if not env_read or not values.get("SEARXNG_API_TOKEN") else "PASS"),
        ("SEARXNG_APPROVAL_DISABLED", "PASS" if env_read and values.get("SEARXNG_APPROVED", "").casefold() in {"0", "false", "no"} else "BLOCKED"),
        ("DOTENV_GIT_IGNORED", git_status_ok and ignored_result.returncode == 0),
        ("DOTENV_NOT_TRACKED", git_status_ok and tracked_result.returncode == 1),
        ("SCANNER_GIT_STATUS", git_status_ok),
        ("TRACKED_ENV_FILE_SCAN", scan_ok and not any(category == "tracked_env_file" for _, category, _ in findings)),
        ("TRACKED_SOURCE_CREDENTIAL_SCAN", scan_ok and not findings),
        ("NEW_CHANGE_SECRET_SCAN", scan_ok and changed_ok and not new_findings),
        ("BASELINE_SECRET_FINDINGS_RECORDED", scan_ok and changed_ok),
        ("PR_DELTA_SECRET_SCAN", scan_ok and changed_ok and not delta_findings),
    ]


def main() -> int:
    try:
        checks = run_checks()
    except Exception:
        print("PREFLIGHT_INTERNAL_ERROR: FAIL")
        return 2
    failed = False
    for label, result in checks:
        if isinstance(result, bool):
            rendered = "PASS" if result else "FAIL"
            failed = failed or not result
        else:
            rendered = result
            failed = failed or result == "FAIL"
        print(f"{label}: {rendered}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
