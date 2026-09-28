"""Static guardrails for the seven-app Slack deployment contract."""

from __future__ import annotations

from pathlib import Path

from hedge.slack_delivery import HedgeRole, ROLE_PROFILES


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_seven_profiles_are_unique_and_specialists_are_outbound_only() -> None:
    assert len(ROLE_PROFILES) == 7
    assert len({profile.token_env for profile in ROLE_PROFILES.values()}) == 7
    cio = _read("slack/manifest.yaml")
    for scope in ("app_mentions:read", "channels:history", "chat:write", "reactions:write"):
        assert scope in cio
    assert "app_mention" in cio and "socket_mode_enabled: true" in cio
    assert "groups:history" not in cio
    for role in HedgeRole:
        if role is HedgeRole.CIO:
            continue
        manifest = _read(f"slack/agent-manifests/{role.value.replace('_', '-')}.yaml")
        assert "- chat:write" in manifest
        assert "socket_mode_enabled: false" in manifest
        assert "app_mention" not in manifest
        assert "channels:history" not in manifest
        assert "reactions:write" not in manifest
        assert "im:history" not in manifest


def test_checkout_contains_no_slack_token_values() -> None:
    files = [
        *ROOT.glob("src/hedge/*.py"),
        *ROOT.glob("slack/**/*.yaml"),
        *ROOT.glob("deploy/*"),
        *ROOT.glob("docs/*.md"),
        *ROOT.glob("tests/*.py"),
    ]
    forbidden = ("x" + "oxb-", "x" + "app-", "x" + "oxp-", "x" + "oxa-")
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files if path.is_file())
    assert not any(marker in combined for marker in forbidden)
