"""Mixer, mapping and loss handling against a fake rig; no program, no network."""

import asyncio
import json

import pytest
from ai_agent_application.avatar import (
    Avatar,
    AvatarError,
    Emotion,
    ModelInfo,
    ParameterRange,
    load_profile,
    profile_from_json,
)

INFO = ModelInfo(
    "Test",
    hotkeys={"Smile": "h1", "Frown": "h2"},
    expressions=frozenset({"angry.exp3.json"}),
    parameters={"MouthOpen": ParameterRange(0, 1), "Blush": ParameterRange(0, 2)},
)
PROFILE = {
    "version": 1,
    "emotions": {
        "happy": {"hotkey": "Smile"},
        "angry": {"expression": "angry.exp3.json"},
        "embarrassed": {"parameters": {"Blush": 5}},
        "sad": {"hotkey": "Missing"},
    },
    "neutral": {"hotkey": "Frown"},
    "mouth_parameter": "MouthOpen",
}


class Rig:
    def __init__(self, info=INFO):
        self.info, self.calls, self.fail, self.down = info, [], False, False

    async def connect(self):
        if self.down:
            raise AvatarError("vts:connect")
        self.calls.append(("connect",))
        return self.info

    async def hotkey(self, hotkey_id):
        self._check()
        self.calls.append(("hotkey", hotkey_id))

    async def expression(self, file, *, active):
        self._check()
        self.calls.append(("expression", file, active))

    async def inject(self, values):
        self._check()
        self.calls.append(("inject", dict(values)))

    async def close(self):
        self.calls.append(("close",))

    def _check(self):
        if self.fail:
            raise AvatarError("vts:transport")


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def make(rig=None, **kwargs):
    rig, clock = rig or Rig(), Clock()
    return Avatar(rig, profile_from_json(PROFILE), clock=clock, **kwargs), rig, clock


def run(coro):
    return asyncio.run(coro)


def test_emotion_maps_to_hotkey_and_repeats_are_free():
    async def scenario():
        avatar, rig, clock = make()
        assert (await avatar.express(Emotion.HAPPY)).applied
        assert ("hotkey", "h1") in rig.calls
        assert not (await avatar.express(Emotion.HAPPY)).applied
        assert rig.calls.count(("hotkey", "h1")) == 1

    run(scenario())


def test_changes_are_paced_and_expression_is_switched_off_before_the_next():
    async def scenario():
        avatar, rig, clock = make()
        await avatar.express(Emotion.ANGRY)
        assert "Слишком часто" in (await avatar.express(Emotion.HAPPY)).text
        clock.now += 2
        await avatar.express(Emotion.HAPPY)
        assert [c for c in rig.calls if c[0] != "connect"] == [
            ("expression", "angry.exp3.json", True),
            ("expression", "angry.exp3.json", False),
            ("hotkey", "h1"),
        ]

    run(scenario())


def test_parameters_are_clamped_to_the_rig_range():
    async def scenario():
        avatar, rig, _ = make()
        await avatar.express(Emotion.EMBARRASSED)
        assert ("inject", {"Blush": 2.0}) in rig.calls

    run(scenario())


def test_mapping_problems_are_reported_and_unmapped_emotions_are_refused():
    async def scenario():
        avatar, _, clock = make()
        await avatar.express(Emotion.HAPPY)
        assert avatar.problems == ["sad:hotkey:Missing"]
        clock.now += 2
        out = await avatar.express(Emotion.SURPRISED)
        assert not out.applied and "нет выражения" in out.text

    run(scenario())


def test_unreachable_rig_is_a_message_not_an_error_and_retries_are_spaced():
    async def scenario():
        rig = Rig()
        rig.down = True
        avatar, _, clock = make(rig)
        assert "недоступен" in (await avatar.express(Emotion.HAPPY)).text
        rig.down = False
        assert "недоступен" in (await avatar.express(Emotion.HAPPY)).text  # still waiting
        clock.now += 31
        assert (await avatar.express(Emotion.HAPPY)).applied

    run(scenario())


def test_reconnect_starts_from_neutral_and_never_replays_a_hotkey():
    async def scenario():
        avatar, rig, clock = make()
        await avatar.express(Emotion.HAPPY)
        rig.fail = True
        clock.now += 2
        assert not (await avatar.express(Emotion.ANGRY)).applied
        assert not avatar.available
        rig.fail = False
        before = list(rig.calls)
        clock.now += 31
        await avatar.express(Emotion.THINKING)  # unmapped, so only the reconnect happens
        assert avatar.emotion is Emotion.NEUTRAL
        assert [c for c in rig.calls[len(before) :] if c[0] == "hotkey"] == []

    run(scenario())


def test_mouth_follows_levels_is_rate_limited_and_neutral_closes_it():
    async def scenario():
        avatar, rig, clock = make()
        await avatar.express(Emotion.HAPPY)
        await avatar.mouth(0.8)
        await avatar.mouth(0.1)  # same instant: dropped
        clock.now += 0.1
        await avatar.mouth(0.8)  # barely changed: dropped
        assert [c for c in rig.calls if c[0] == "inject"] == [("inject", {"MouthOpen": 0.8})]
        await avatar.neutral()
        assert ("inject", {"MouthOpen": 0.0}) in rig.calls
        assert ("hotkey", "h2") in rig.calls and avatar.emotion is Emotion.NEUTRAL

    run(scenario())


def test_neutral_never_raises_when_the_rig_is_gone():
    async def scenario():
        avatar, rig, _ = make()
        await avatar.express(Emotion.HAPPY)
        rig.fail = True
        await avatar.neutral()
        assert not avatar.available and avatar.emotion is Emotion.NEUTRAL

    run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        {"version": 2},
        {"version": 1, "extra": 1},
        {"version": 1, "emotions": {"joy": {}}},
        {"version": 1, "emotions": {"happy": {"hotkey": ""}}},
        {"version": 1, "emotions": {"happy": {"parameters": {"A": True}}}},
        {"version": 1, "mouth_gain": 0},
        {"version": 1, "emotions": {"happy": {"oops": 1}}},
    ],
)
def test_bad_profiles_fail_with_fixed_codes(raw):
    with pytest.raises(AvatarError) as error:
        profile_from_json(raw)
    assert error.value.code.startswith("profile:")


def test_profile_file_rules(tmp_path):
    good = tmp_path / "p.json"
    good.write_text(json.dumps(PROFILE), encoding="utf-8")
    assert load_profile(good).mouth_parameter == "MouthOpen"
    link = tmp_path / "link.json"
    link.symlink_to(good)
    with pytest.raises(AvatarError):
        load_profile(link)
    with pytest.raises(AvatarError):
        load_profile(tmp_path / "missing.json")
