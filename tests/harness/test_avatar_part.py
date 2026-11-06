"""AvatarPart keeps the resting face while she runs and returns to neutral on STOP."""

import asyncio

from ai_agent_core.local_host.harness import AvatarPart


class FakeAvatar:
    def __init__(self):
        self.holds, self.neutrals, self.broken = 0, 0, False

    async def hold(self):
        self.holds += 1
        if self.broken:
            raise RuntimeError("boom")

    async def neutral(self):
        self.neutrals += 1


def test_keeps_holding_until_closed_then_goes_neutral():
    async def scenario():
        avatar = FakeAvatar()
        part = AvatarPart(avatar)  # type: ignore[arg-type]
        part.EVERY = 0.01
        part.tick()
        part.tick()  # a second tick never starts a second loop
        await asyncio.sleep(0.1)
        await part.close()
        held = avatar.holds
        await asyncio.sleep(0.05)
        return held, avatar.holds, avatar.neutrals

    held, after, neutrals = asyncio.run(scenario())
    assert held >= 3 and after == held and neutrals == 1


def test_a_failing_hold_does_not_stop_the_loop():
    async def scenario():
        avatar = FakeAvatar()
        avatar.broken = True
        part = AvatarPart(avatar)  # type: ignore[arg-type]
        part.EVERY = 0.01
        part.tick()
        await asyncio.sleep(0.1)
        await part.close()
        return avatar.holds

    assert asyncio.run(scenario()) >= 3
