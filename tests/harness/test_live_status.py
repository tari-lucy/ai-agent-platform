"""The panel's view of the live links: states, details, and the contract that carries them."""

from types import SimpleNamespace

import pytest
from ai_agent_contracts.panel import HarnessStatus, LiveLink
from pydantic import ValidationError

from ai_agent_core.m3_composition.harness_host import HarnessHost


def host(**parts):
    base = {"avatar": None, "speaker": None, "listener": None, "stage": None, "chats": None}
    return SimpleNamespace(**{**base, **parts})


def test_nothing_configured_means_no_badges():
    assert HarnessHost.live(host()) == []  # type: ignore[arg-type]


def test_every_configured_link_reports_a_state():
    avatar = SimpleNamespace(state="down", problems=["x", "y"])
    speaker = SimpleNamespace(spoken_today=lambda: 120)
    listener = SimpleNamespace(on=True, status=lambda: "Слушаю. Само выключится через 90 мин.")
    stage = SimpleNamespace(state="idle")
    chats = SimpleNamespace(platforms={"youtube": 1, "twitch": 2}, connected={"twitch"})
    rows = HarnessHost.live(  # type: ignore[arg-type]
        host(avatar=avatar, speaker=speaker, listener=listener, stage=stage, chats=chats)
    )
    assert rows == [
        ("avatar", "down", "несовпадений профиля: 2"),
        ("speech", "idle", "сегодня сказано знаков: 120"),
        ("listening", "on", "Слушаю. Само выключится через 90 мин."),
        ("obs", "idle", ""),
        ("twitch", "on", ""),
        ("youtube", "down", ""),
    ]


def test_the_contract_carries_links_and_refuses_unknown_ones():
    scope = {"workspace_id": "w", "persona_id": "p", "memory_space_id": "m", "memory_mode": "work"}
    status = HarnessStatus(
        scope=scope,  # type: ignore[arg-type]
        enabled=True,
        ready=True,
        live=[LiveLink(name="avatar", state="on")],
    )
    assert status.live[0].detail == ""
    with pytest.raises(ValidationError):
        LiveLink(name="camera", state="on")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LiveLink(name="obs", state="on", detail="x" * 121)
