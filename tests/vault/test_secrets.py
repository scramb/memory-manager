# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for secret detection (`vault/secrets.py`).

Fixtures use fake, syntactically-valid-looking secrets built from string
concatenation (never a literal secret-shaped string) so push protection and
other scanners never flag this repository. The well-known public test IBAN
and test credit card number are used for those two rules.
"""

from __future__ import annotations

import pytest

from memory_manager.vault.secrets import Finding, SecretFound, check, load_rules, scan

# Fake secrets, assembled at runtime so no secret-shaped literal sits in the
# source. None of these are real credentials.
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_FAKE_AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
_FAKE_GCP_API_KEY = "AIza" + "A" * 35
_FAKE_AZURE_STORAGE_KEY = "A" * 40 + "=="
_FAKE_AZURE_CLIENT_SECRET = "Q~8vZ3kLp" + "R7tX2mN9bY1"
_FAKE_GITHUB_PAT = "ghp_" + "A" * 36
_FAKE_GITHUB_APP_TOKEN = "gho_" + "B" * 36
_FAKE_GITLAB_PAT = "glpat-" + "C" * 20
_FAKE_SLACK_TOKEN = "xoxb-" + "1" * 20
_FAKE_SLACK_WEBHOOK = (
    "https://hooks.slack.com/services/T" + "A" * 9 + "/B" + "B" * 9 + "/" + "C" * 24
)
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVP-" + "m" * 30
)
_FAKE_GENERIC_VALUE = "tR7$mK9!qX2@wZ5#"
_TEST_IBAN = "DE89 3704 0044 0532 0130 00"  # well-known public test IBAN
_TEST_CREDIT_CARD = "4111 1111 1111 1111"  # well-known public test card number


def _wrap(content: str) -> str:
    """Put `content` on line 2, surrounded by unrelated context lines."""
    return f"context before\n{content}\ncontext after\n"


def _rule_ids(findings: list[Finding]) -> set[str]:
    return {finding.rule_id for finding in findings}


# rule id -> (text containing a match, text that must not match that rule)
_FIXTURES: dict[str, tuple[str, str]] = {
    "private-key": (
        _wrap(
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBOgIBAAJBAKfakefakefakefakefakefakefakefakefakefakefake==\n"
            "-----END RSA PRIVATE KEY-----"
        ),
        _wrap("-----BEGIN CERTIFICATE-----\nMIIBfakefakefake==\n-----END CERTIFICATE-----"),
    ),
    "aws-access-key-id": (
        _wrap(f"AWS_ACCESS_KEY_ID={_FAKE_AWS_ACCESS_KEY_ID}"),
        _wrap("AWS_ACCESS_KEY_ID=AKIASHORT"),
    ),
    "aws-secret-access-key": (
        _wrap(f'aws_secret_access_key = "{_FAKE_AWS_SECRET_ACCESS_KEY}"'),
        _wrap('aws_secret_access_key = "short"'),
    ),
    "gcp-service-account": (
        _wrap('{"type": "service_account", "project_id": "x", "private_key": "y"}'),
        _wrap('{"type": "service_account", "project_id": "x"}'),
    ),
    "gcp-api-key": (
        _wrap(f"key={_FAKE_GCP_API_KEY}"),
        _wrap("key=AIzaShort"),
    ),
    "azure-storage-key": (
        _wrap(f"AccountKey={_FAKE_AZURE_STORAGE_KEY}"),
        _wrap("AccountKey=short"),
    ),
    "azure-client-secret": (
        _wrap(f'azure_client_secret = "{_FAKE_AZURE_CLIENT_SECRET}"'),
        _wrap('azure_client_secret = "aaaaaaaaaaaaaaaa"'),
    ),
    "github-pat": (
        _wrap(f"token={_FAKE_GITHUB_PAT}"),
        _wrap("token=ghp_tooshort"),
    ),
    "github-app-token": (
        _wrap(f"token={_FAKE_GITHUB_APP_TOKEN}"),
        _wrap("token=gho_tooshort"),
    ),
    "gitlab-pat": (
        _wrap(f"token={_FAKE_GITLAB_PAT}"),
        _wrap("token=glpat-tooshort"),
    ),
    "slack-token": (
        _wrap(f"token={_FAKE_SLACK_TOKEN}"),
        _wrap("token=xoxq-11111111111111111111"),
    ),
    "slack-webhook": (
        _wrap(_FAKE_SLACK_WEBHOOK),
        _wrap("https://hooks.slack.com/services/invalid"),
    ),
    "jwt": (
        _wrap(f"auth={_FAKE_JWT}"),
        _wrap("auth=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
    ),
    "generic-assignment": (
        _wrap(f"password = {_FAKE_GENERIC_VALUE}"),
        _wrap("password = aaaaaaaaaaaa"),
    ),
    "iban": (
        _wrap(f"iban: {_TEST_IBAN}"),
        _wrap("iban: DE99 3704 0044 0532 0130 00"),
    ),
    "credit-card": (
        _wrap(f"card: {_TEST_CREDIT_CARD}"),
        _wrap("card: 4111-1111-1111-1112"),
    ),
}


class TestRuleFile:
    def test_parses_into_rules(self) -> None:
        rule_set = load_rules()
        assert len(rule_set.rules) > 0

    def test_every_rule_id_is_unique(self) -> None:
        rule_set = load_rules()
        ids = [rule.id for rule in rule_set.rules]
        assert len(ids) == len(set(ids))

    def test_fixtures_cover_every_rule(self) -> None:
        rule_set = load_rules()
        rule_ids = {rule.id for rule in rule_set.rules}
        assert set(_FIXTURES) == rule_ids


@pytest.mark.parametrize("rule_id", sorted(_FIXTURES))
class TestEachRule:
    def test_positive_fixture_is_detected(self, rule_id: str) -> None:
        positive_text, _ = _FIXTURES[rule_id]
        assert rule_id in _rule_ids(scan(positive_text))

    def test_negative_fixture_is_not_detected(self, rule_id: str) -> None:
        _, negative_text = _FIXTURES[rule_id]
        assert rule_id not in _rule_ids(scan(negative_text))

    def test_positive_fixture_reports_the_right_line(self, rule_id: str) -> None:
        positive_text, _ = _FIXTURES[rule_id]
        findings = [f for f in scan(positive_text) if f.rule_id == rule_id]
        assert all(f.line == 2 for f in findings)

    def test_positive_fixture_raises_secret_found(self, rule_id: str) -> None:
        positive_text, _ = _FIXTURES[rule_id]
        with pytest.raises(SecretFound):
            check(positive_text)

    def test_negative_fixture_does_not_raise_for_this_rule(self, rule_id: str) -> None:
        _, negative_text = _FIXTURES[rule_id]
        findings = scan(negative_text)
        assert rule_id not in _rule_ids(findings)


class TestMessageNeverEchoesTheSecret:
    @pytest.mark.parametrize(
        ("rule_id", "needle"),
        [
            ("aws-access-key-id", _FAKE_AWS_ACCESS_KEY_ID),
            ("aws-secret-access-key", _FAKE_AWS_SECRET_ACCESS_KEY),
            ("gcp-api-key", _FAKE_GCP_API_KEY),
            ("azure-storage-key", _FAKE_AZURE_STORAGE_KEY),
            ("azure-client-secret", _FAKE_AZURE_CLIENT_SECRET),
            ("github-pat", _FAKE_GITHUB_PAT),
            ("github-app-token", _FAKE_GITHUB_APP_TOKEN),
            ("gitlab-pat", _FAKE_GITLAB_PAT),
            ("slack-token", _FAKE_SLACK_TOKEN),
            ("jwt", _FAKE_JWT),
            ("generic-assignment", _FAKE_GENERIC_VALUE),
            ("iban", _TEST_IBAN),
            ("credit-card", _TEST_CREDIT_CARD),
        ],
    )
    def test_secret_value_is_absent_from_the_message(self, rule_id: str, needle: str) -> None:
        positive_text, _ = _FIXTURES[rule_id]
        with pytest.raises(SecretFound) as excinfo:
            check(positive_text)
        assert needle not in str(excinfo.value)

    def test_finding_dataclass_has_no_text_field(self) -> None:
        finding = Finding(rule_id="x", description="x", line=1)
        assert not hasattr(finding, "text")
        assert not hasattr(finding, "match")


class TestCheck:
    def test_clean_text_does_not_raise(self) -> None:
        check("Just a normal note about groceries and plans.")

    def test_findings_are_sorted_by_line(self) -> None:
        text = f"{_FIXTURES['github-pat'][0]}more context\ntoken={_FAKE_SLACK_TOKEN}\n"
        findings = scan(text)
        lines = [f.line for f in findings]
        assert lines == sorted(lines)

    def test_scan_returns_findings_without_raising(self) -> None:
        positive_text, _ = _FIXTURES["github-pat"]
        findings = scan(positive_text)
        assert any(f.rule_id == "github-pat" for f in findings)

    def test_secret_found_carries_every_finding(self) -> None:
        text = f"token={_FAKE_GITHUB_PAT}\nmore\ntoken={_FAKE_SLACK_TOKEN}\n"
        with pytest.raises(SecretFound) as excinfo:
            check(text)
        assert len(excinfo.value.findings) >= 2


class TestAllowlist:
    def test_env_var_reference_is_not_reported(self) -> None:
        text = _wrap("password = ${SECRETS_MANAGER:prod/db/password}")
        findings = scan(text)
        assert "generic-assignment" not in _rule_ids(findings)

    def test_placeholder_x_value_is_not_reported(self) -> None:
        text = _wrap("token = xxxxxxxxxxxxx")
        findings = scan(text)
        assert "generic-assignment" not in _rule_ids(findings)
