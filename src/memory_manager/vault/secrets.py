# SPDX-License-Identifier: AGPL-3.0-only
"""Scans note content for secrets before it is written to the vault.

CLAUDE.md requires a secret scan before every commit. This module applies
a gitleaks-style rule set (`secrets_rules.toml`) to the raw text of a note:
private keys, cloud provider credentials, GitHub/GitLab/Slack tokens, JWTs,
generic high-entropy assignments, IBANs and credit card numbers.

`scan()` returns every match; `check()` raises `SecretFound` if there is at
least one. The exception message - and every `Finding` - names the rule and
the line, never the matched text, so a rejected write never echoes the
secret back into a log or an error page.
"""

from __future__ import annotations

import math
import re
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["Finding", "SecretFound", "check", "load_rules", "scan"]

_RULES_PATH = Path(__file__).with_name("secrets_rules.toml")


@dataclass(frozen=True)
class _Rule:
    """A compiled rule from `secrets_rules.toml`."""

    id: str
    description: str
    pattern: re.Pattern[str]
    group: int
    entropy: float | None
    keywords: tuple[str, ...]
    validator: Callable[[str], bool] | None


@dataclass(frozen=True)
class _RuleSet:
    rules: tuple[_Rule, ...]
    allow: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class Finding:
    """One place in scanned text that looks like a secret.

    Never carries the matched text itself - only enough to tell a human
    where to look and what the rule thinks it found.
    """

    rule_id: str
    description: str
    line: int


class SecretFound(ValueError):
    """Raised by `check` when `scan` finds at least one secret.

    `findings` holds every match, not just the first. The message reports
    only the first finding's rule and line and never the matched text.
    """

    def __init__(self, findings: tuple[Finding, ...]) -> None:
        self.findings = findings
        first = findings[0]
        super().__init__(
            f"line {first.line} looks like {first.description} (rule {first.rule_id}) - "
            "remove the secret; memory must not store credentials. Store a reference "
            "to where it is kept instead."
        )


def _iban_checksum_valid(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        digits = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(digits) % 97 == 1


def _luhn_valid(value: str) -> bool:
    digits = [int(ch) for ch in value if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "iban": _iban_checksum_valid,
    "credit-card": _luhn_valid,
}


def _shannon_entropy(value: str) -> float:
    """Shannon entropy of `value` in bits per character, 0.0 for ''."""
    if not value:
        return 0.0
    length = len(value)
    counts = Counter(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _compile_rule(entry: dict[str, Any]) -> _Rule:
    flags = re.DOTALL if entry.get("dotall", False) else 0
    rule_id = str(entry["id"])
    return _Rule(
        id=rule_id,
        description=str(entry["description"]),
        pattern=re.compile(str(entry["regex"]), flags),
        group=int(entry.get("group", 0)),
        entropy=float(entry["entropy"]) if "entropy" in entry else None,
        keywords=tuple(str(keyword).lower() for keyword in entry.get("keywords", [])),
        validator=_VALIDATORS.get(rule_id),
    )


@lru_cache(maxsize=1)
def load_rules() -> _RuleSet:
    """Load and compile the rule set from `secrets_rules.toml` (cached)."""
    data: dict[str, Any] = tomllib.loads(_RULES_PATH.read_text(encoding="utf-8"))
    rules = tuple(_compile_rule(entry) for entry in data.get("rule", []))
    allow = tuple(re.compile(str(entry["regex"])) for entry in data.get("allow", []))
    return _RuleSet(rules=rules, allow=allow)


def _is_allowed(candidate: str, allow_patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.fullmatch(candidate) for pattern in allow_patterns)


def scan(text: str) -> list[Finding]:
    """Scan `text` for anything that looks like a secret.

    Returns one `Finding` per match, ordered by line. Findings never carry
    the matched text - only the rule id, its description and the line
    number the match starts on.
    """
    rule_set = load_rules()
    lower_text = text.lower()
    findings: list[Finding] = []

    for rule in rule_set.rules:
        if rule.keywords and not any(keyword in lower_text for keyword in rule.keywords):
            continue
        for match in rule.pattern.finditer(text):
            candidate = match.group(rule.group) if rule.group else match.group(0)
            if _is_allowed(candidate, rule_set.allow):
                continue
            if rule.entropy is not None and _shannon_entropy(candidate) < rule.entropy:
                continue
            if rule.validator is not None and not rule.validator(candidate):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(rule_id=rule.id, description=rule.description, line=line))

    findings.sort(key=lambda finding: finding.line)
    return findings


def check(text: str) -> None:
    """Raise `SecretFound` if `scan(text)` finds at least one secret."""
    findings = scan(text)
    if findings:
        raise SecretFound(tuple(findings))
