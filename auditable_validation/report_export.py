#!/usr/bin/env python3
"""Export developer reports with deterministic secret redaction."""
from __future__ import annotations

import re
from pathlib import Path

_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(cookie\s*:\s*)[^\n]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password|passwd|token|secret)(\s*[=:]\s*)[^\s,;]+"), r"\1\2[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def export_patch_advisory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(redact(source.read_text(encoding="utf-8")), encoding="utf-8")
    destination.chmod(0o640)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export_patch_advisory(args.source, args.destination)
