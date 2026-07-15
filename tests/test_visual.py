"""Tests for the advisory VLM screenshot critique (findings only, never a gate)."""
import logging
import os

from kickass_loop_engineer.providers.base import Provider, ProviderError, ProviderResult
from kickass_loop_engineer.visual import VISUAL_SYSTEM, visual_critique

from helpers import FakePolicy, FakeVisionProvider

CMD = "npx playwright-cli screenshot http://localhost:3000 shot.png"


class _RaisingVisionProvider(Provider):
    name = "raising-vision"

    def complete(self, system, user, images=()):
        raise ProviderError("vision endpoint down")


class _TextOnlyProvider(Provider):
    """A provider whose ``complete`` has no images param (misconfiguration)."""

    name = "text-only"

    def complete(self, system, user):
        return ProviderResult(text="NO FINDINGS")


def _workspace(tmp_path, shot="shot.png"):
    ws = tmp_path / "ws"
    ws.mkdir()
    if shot:
        (ws / shot).write_bytes(b"png bytes")
    return str(ws)


def test_happy_path_returns_parsed_findings(tmp_path):
    ws = _workspace(tmp_path)
    provider = FakeVisionProvider(
        text="FINDING: save button overlaps footer\nFINDING: contrast too low")
    findings = visual_critique(CMD, ws, FakePolicy(), provider, "ship the form")
    assert findings == ["save button overlaps footer", "contrast too low"]


def test_screenshot_command_runs_via_policy_in_workspace(tmp_path):
    ws = _workspace(tmp_path)
    policy = FakePolicy()
    visual_critique(CMD, ws, policy, FakeVisionProvider(), "obj")
    assert policy.runs == [(CMD, ws)]


def test_provider_gets_visual_system_objective_and_image_path(tmp_path):
    ws = _workspace(tmp_path)
    provider = FakeVisionProvider()
    visual_critique(CMD, ws, FakePolicy(), provider, "ship the login form")
    system, user, images = provider.calls[0]
    assert system == VISUAL_SYSTEM
    assert "ship the login form" in user
    assert images == (os.path.join(ws, "shot.png"),)


def test_absolute_output_path_in_command_is_used_verbatim(tmp_path):
    ws = _workspace(tmp_path, shot=None)
    shot = tmp_path / "elsewhere.png"
    shot.write_bytes(b"png")
    provider = FakeVisionProvider(text="FINDING: x")
    findings = visual_critique(
        f"npx playwright-cli screenshot http://localhost:3000 {shot}",
        ws, FakePolicy(), provider, "obj")
    assert findings == ["x"]
    assert provider.calls[0][2] == (str(shot),)


def test_no_findings_reply_yields_empty_list(tmp_path):
    ws = _workspace(tmp_path)
    findings = visual_critique(CMD, ws, FakePolicy(), FakeVisionProvider(), "obj")
    assert findings == []


def test_policy_rejection_warns_and_skips_the_provider(tmp_path, caplog):
    ws = _workspace(tmp_path)
    provider = FakeVisionProvider()
    policy = FakePolicy(passed=False, error="command not on the allowlist")
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.visual"):
        findings = visual_critique(CMD, ws, policy, provider, "obj")
    assert findings == []
    assert provider.calls == []
    assert any("allowlist" in r.message for r in caplog.records)


def test_failed_screenshot_command_warns_and_returns_empty(tmp_path, caplog):
    ws = _workspace(tmp_path)
    provider = FakeVisionProvider()
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.visual"):
        findings = visual_critique(CMD, ws, FakePolicy(passed=False), provider, "obj")
    assert findings == []
    assert provider.calls == []
    assert any("screenshot" in r.message for r in caplog.records)


def test_missing_output_file_warns_and_returns_empty(tmp_path, caplog):
    ws = _workspace(tmp_path, shot=None)  # command "passes" but writes nothing
    provider = FakeVisionProvider()
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.visual"):
        findings = visual_critique(CMD, ws, FakePolicy(), provider, "obj")
    assert findings == []
    assert provider.calls == []
    assert any("shot.png" in r.message for r in caplog.records)


def test_provider_error_never_raises(tmp_path, caplog):
    ws = _workspace(tmp_path)
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.visual"):
        findings = visual_critique(CMD, ws, FakePolicy(), _RaisingVisionProvider(), "obj")
    assert findings == []
    assert any("vision endpoint down" in r.message for r in caplog.records)


def test_text_only_provider_is_a_config_error_degraded_to_warning(tmp_path, caplog):
    ws = _workspace(tmp_path)
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.visual"):
        findings = visual_critique(CMD, ws, FakePolicy(), _TextOnlyProvider(), "obj")
    assert findings == []
    assert caplog.records, "misconfigured provider must log a warning"


def test_none_policy_falls_back_to_default_which_rejects_unlisted_commands(tmp_path):
    ws = _workspace(tmp_path)
    provider = FakeVisionProvider()
    findings = visual_critique(
        "definitely-not-allowlisted --now shot.png", ws, None, provider, "obj")
    assert findings == []
    assert provider.calls == []


def test_on_usage_receives_the_provider_result_for_ledgering(tmp_path):
    ws = _workspace(tmp_path)
    seen = []
    visual_critique(CMD, ws, FakePolicy(), FakeVisionProvider(cost_usd=0.25),
                    "obj", on_usage=seen.append)
    assert len(seen) == 1
    assert seen[0].cost_usd == 0.25


def test_on_usage_failure_never_escapes(tmp_path, caplog):
    ws = _workspace(tmp_path)

    def boom(_result):
        raise RuntimeError("ledger exploded")

    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.visual"):
        findings = visual_critique(CMD, ws, FakePolicy(),
                                   FakeVisionProvider(text="FINDING: x"),
                                   "obj", on_usage=boom)
    assert findings == []
    assert any("ledger exploded" in r.message for r in caplog.records)
