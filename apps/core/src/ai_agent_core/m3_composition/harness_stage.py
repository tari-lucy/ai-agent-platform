"""The stream's stage for the persona (doc37 A3): what is live, and which scene is on.

She may see the state of OBS and switch between the scenes the operator made. She cannot start
or end a broadcast or a recording: that stays the operator's (D92), and the adapter has no such
request at all. A lost program is a plain message, retried at most every ``retry_after`` seconds.
Scene names are the operator's own data. Switching is paced so a loop cannot flicker the stream.
"""

import logging
import time
from collections.abc import Callable
from typing import Protocol

from ai_agent_adapters.stream.obs import ObsError, ObsState

LOG = logging.getLogger(__name__)
MIN_SWITCH = 3.0  # seconds between two scene changes
RETRY_AFTER = 30.0


class Obs(Protocol):
    async def connect(self) -> None: ...

    async def state(self) -> ObsState: ...

    async def set_scene(self, name: str) -> None: ...

    async def close(self) -> None: ...


class Stage:
    def __init__(
        self,
        obs: Obs,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_switch: float = MIN_SWITCH,
        retry_after: float = RETRY_AFTER,
    ) -> None:
        self.obs, self.clock = obs, clock
        self.min_switch, self.retry_after = min_switch, retry_after
        self._up = False
        self._down_until = -1e9
        self._switched = -1e9

    async def _ready(self) -> bool:
        if self._up:
            return True
        if self.clock() < self._down_until:
            return False
        try:
            await self.obs.connect()
        except ObsError as error:
            LOG.warning("OBS unavailable: %s", error.code)
            self._down_until = self.clock() + self.retry_after
            return False
        self._up = True
        return True

    async def _lost(self, error: ObsError) -> str:
        LOG.warning("OBS lost: %s", error.code)
        self._up = False
        self._down_until = self.clock() + self.retry_after
        await self.obs.close()
        return "OBS сейчас недоступен: программа не на связи."

    async def status(self) -> str:
        if not await self._ready():
            return "OBS сейчас недоступен: программа не на связи."
        try:
            state = await self.obs.state()
        except ObsError as error:
            return await self._lost(error)
        return (
            f"Эфир: {'идёт' if state.streaming else 'не идёт'}; запись: "
            f"{'идёт' if state.recording else 'не идёт'}. Сейчас сцена «{state.scene}». "
            "Сцены: " + ", ".join(f"«{s}»" for s in state.scenes) + "."
        )

    async def scene(self, name: str) -> str:
        name = name.strip()
        if not await self._ready():
            return "OBS сейчас недоступен: программа не на связи."
        try:
            state = await self.obs.state()
            if name not in state.scenes:
                have = ", ".join(f"«{s}»" for s in state.scenes)
                return f"Нет сцены «{name[:60]}». Есть: {have}."
            if name == state.scene:
                return f"Сцена «{name}» уже включена."
            if self.clock() - self._switched < self.min_switch:
                return "Слишком часто меняю сцену, пропущено."
            await self.obs.set_scene(name)
        except ObsError as error:
            return await self._lost(error)
        self._switched = self.clock()
        return f"Включена сцена «{name}»."

    async def close(self) -> None:
        self._up = False
        await self.obs.close()
