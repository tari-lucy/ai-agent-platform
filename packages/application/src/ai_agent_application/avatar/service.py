"""The avatar as one service: emotions through a mixer, mouth from real audio, safe neutral.

Rules (doc04, doc26 F3, doc37):
- An emotion is not copied from the model stream into every facial command: a change is
  accepted at most once per ``min_interval`` and the same emotion is a no-op.
- The mouth follows the level of audio that is actually playing; a missing or cancelled
  speech ends with the mouth closed.
- A lost connection never reaches the model as an error and never breaks the platform: the
  avatar is simply unavailable until the next attempt, and a reconnect starts from neutral
  and checks the mapping again (the model may have been swapped). An old hotkey is never
  replayed after a reconnect.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .model import AvatarError, AvatarPort, Emotion, Look, ModelInfo, Profile

LOG = logging.getLogger(__name__)
MIN_INTERVAL = 1.5  # seconds between accepted emotion changes
MOUTH_HZ = 20.0  # at most this many mouth frames per second
MOUTH_STEP = 0.03  # smaller changes of the mouth are not worth a frame
RETRY_AFTER = 4.0  # seconds before a lost rig is tried again (a recording cannot sleep for 30)


@dataclass(frozen=True)
class Outcome:
    """The text the persona gets back: what happened, never a transport detail."""

    applied: bool
    text: str


class Avatar:
    def __init__(
        self,
        port: AvatarPort,
        profile: Profile,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_INTERVAL,
        retry_after: float = RETRY_AFTER,
    ) -> None:
        self.port, self.profile, self.clock = port, profile, clock
        self.min_interval, self.retry_after = min_interval, retry_after
        self.problems: list[str] = []
        self._info: ModelInfo | None = None
        self._lock = asyncio.Lock()
        self._current = Emotion.NEUTRAL
        self._expression: str | None = None
        self._held: dict[str, float] = {}
        self._rest: dict[str, float] = {}
        self._changed = -1e9
        self._down_until = -1e9
        self._mouth = 0.0
        self._mouth_at = -1e9

    @property
    def available(self) -> bool:
        return self._info is not None

    @property
    def state(self) -> str:
        """``on`` connected; ``down`` the program was tried and is not there; ``idle`` not tried."""
        if self._info is not None:
            return "on"
        return "down" if self._down_until > -1e8 else "idle"

    @property
    def emotion(self) -> Emotion:
        return self._current

    async def _ensure(self) -> bool:
        """Connected and mapped, or one attempt per ``retry_after``. Caller holds the lock."""
        if self._info is not None:
            return True
        if self.clock() < self._down_until:
            return False
        try:
            info = await self.port.connect()
        except (AvatarError, OSError, TimeoutError) as error:
            LOG.warning("Avatar unavailable: %s", getattr(error, "code", type(error).__name__))
            self._down_until = self.clock() + self.retry_after
            return False
        self._info = info
        self.problems = self.profile.validate(info)
        # A new session starts from neutral and replays nothing from before the loss.
        self._current, self._expression, self._held = Emotion.NEUTRAL, None, {}
        self._rest = {
            k: min(max(v, info.parameters[k].minimum), info.parameters[k].maximum)
            for k, v in self.profile.rest.items()
            if k in info.parameters
        }
        self._changed, self._mouth = -1e9, 0.0
        if self.problems:
            LOG.warning("Avatar mapping does not fit the model: %s", ", ".join(self.problems))
        return True

    async def _lost(self, error: BaseException | None = None) -> None:
        if error is not None:
            LOG.warning("Avatar lost: %s", getattr(error, "code", type(error).__name__))
        self._info = None
        self._down_until = self.clock() + self.retry_after
        try:
            await self.port.close()
        except AvatarError, OSError, TimeoutError:
            pass

    def _usable(self, look: Look) -> Look:
        """The part of a look the loaded rig really has; the rest is dropped, not guessed."""
        info = self._info
        if info is None:
            return Look()
        return Look(
            look.hotkey if look.hotkey in info.hotkeys else None,
            look.expression if look.expression in info.expressions else None,
            {k: v for k, v in look.parameters.items() if k in info.parameters},
        )

    async def _apply(self, look: Look) -> None:
        info, look = self._info, self._usable(look)
        if info is None:
            return
        if self._expression is not None and self._expression != look.expression:
            await self.port.expression(self._expression, active=False)
            self._expression = None
        if look.expression is not None and self._expression != look.expression:
            await self.port.expression(look.expression, active=True)
            self._expression = look.expression
        if look.hotkey is not None:
            await self.port.hotkey(info.hotkeys[look.hotkey])
        self._held = {
            k: min(max(v, info.parameters[k].minimum), info.parameters[k].maximum)
            for k, v in look.parameters.items()
        }
        if self._held:
            await self.port.inject({**self._rest, **self._held})

    async def hold(self) -> None:
        """Re-send the resting parameters (eyes open) so the program keeps them; best effort.

        The program lets go of an injected value about a second after the last one, so this is
        called twice a second while the persona runs. It also connects the avatar at START.
        """
        async with self._lock:
            if not await self._ensure():
                return
            values = {**self._rest, **self._held}
            if not values:
                return
            try:
                await self.port.inject(values)
            except (AvatarError, OSError, TimeoutError) as error:  # fmt: skip
                await self._lost(error)

    async def ready(self) -> bool:
        """Connect now if not connected (one attempt per ``retry_after``); False: no avatar."""
        async with self._lock:
            return await self._ensure()

    async def express(self, emotion: Emotion) -> Outcome:
        async with self._lock:
            if not await self._ensure():
                return Outcome(False, "Аватар сейчас недоступен: программа не на связи.")
            if emotion == self._current:
                return Outcome(False, f"Уже {emotion.value}.")
            if self.clock() - self._changed < self.min_interval:
                return Outcome(False, "Слишком часто меняю выражение, пропущено.")
            look = (
                self.profile.neutral
                if emotion is Emotion.NEUTRAL
                else (self.profile.emotions.get(emotion))
            )
            if look is None:
                return Outcome(False, f"Для {emotion.value} в этой модели нет выражения.")
            shown = self._usable(look)
            if emotion is not Emotion.NEUTRAL and not (
                shown.hotkey or shown.expression or shown.parameters
            ):
                # Mapped, but nothing it names exists in the loaded model: do not claim a feeling
                # was shown that nobody can see.
                return Outcome(False, f"Для {emotion.value} в этой модели нет выражения.")
            try:
                await self._apply(look)
            except (AvatarError, OSError, TimeoutError) as error:  # fmt: skip
                await self._lost(error)
                return Outcome(False, "Аватар сейчас недоступен: связь потеряна.")
            self._current, self._changed = emotion, self.clock()
            return Outcome(True, f"Выражение: {emotion.value}.")

    async def mouth(self, level: float) -> None:
        """One frame of the speech loudness (0..1) of audio that is playing right now."""
        name = self.profile.mouth_parameter
        if name is None or self._info is None:
            return
        info = self._info.parameters.get(name)
        if info is None:
            return
        now = self.clock()
        if now - self._mouth_at < 1.0 / MOUTH_HZ:
            return
        value = min(max(level, 0.0), 1.0) * self.profile.mouth_gain
        value = info.minimum + min(value, 1.0) * (info.maximum - info.minimum)
        if abs(value - self._mouth) < MOUTH_STEP * (info.maximum - info.minimum):
            return
        async with self._lock:
            try:
                await self.port.inject({**self._rest, **self._held, name: value})
            except (AvatarError, OSError, TimeoutError) as error:  # fmt: skip
                await self._lost(error)
                return
            self._mouth, self._mouth_at = value, now

    async def neutral(self) -> None:
        """Speech ended, was cancelled, or STOP: mouth closed, expression back to neutral.

        Best effort and never raises; it works around the mixer's pacing on purpose.
        """
        async with self._lock:
            if self._info is None:
                self._current = Emotion.NEUTRAL
                return
            try:
                name, info = self.profile.mouth_parameter, self._info
                if name is not None and name in info.parameters:
                    closed = info.parameters[name].minimum
                    await self.port.inject({**self._rest, name: closed})
                    self._mouth = closed
                await self._apply(self.profile.neutral)
            except (AvatarError, OSError, TimeoutError) as error:  # fmt: skip
                await self._lost(error)
            self._current, self._changed = Emotion.NEUTRAL, -1e9

    async def close(self) -> None:
        await self.neutral()
        async with self._lock:
            self._info = None
            try:
                await self.port.close()
            except AvatarError, OSError, TimeoutError:
                pass
