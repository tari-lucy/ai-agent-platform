"""Virtual dice outside the LLM: seeded, replayable, only over already admissible options.

The controller filters what is allowed first; only then does the dice choose among those options
or pick a strength inside an allowed range. Seed, context, options and result are kept in a
``Roll`` so a test can replay it. A roll never creates an event, never relaxes a rule and never
decides how another person or the world reacts.
"""

import hashlib
import json
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Roll:
    seed: str
    context: str
    options: tuple[str, ...]
    weights: tuple[float, ...]
    chosen: str
    value: float


class Dice:
    def __init__(self, seed: str) -> None:
        self.seed = seed
        self.log: list[Roll] = []

    def _random(self, context: str) -> random.Random:
        # the stream depends on (seed, context) only, so replays do not depend on earlier rolls
        material = json.dumps([self.seed, context], ensure_ascii=False).encode()
        return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))

    def choose(
        self, context: str, options: tuple[str, ...], weights: tuple[float, ...] | None = None
    ) -> Roll:
        if not options:
            raise ValueError("NO_OPTIONS")
        w = weights if weights is not None else tuple(1.0 for _ in options)
        if len(w) != len(options) or any(x < 0 for x in w) or sum(w) <= 0:
            raise ValueError("BAD_WEIGHTS")
        value = self._random(context).random() * sum(w)
        running = 0.0
        chosen = options[-1]
        for option, weight in zip(options, w, strict=True):
            running += weight
            if value < running:
                chosen = option
                break
        roll = Roll(self.seed, context, options, tuple(w), chosen, round(value, 9))
        self.log.append(roll)
        if len(self.log) > 1000:
            del self.log[0]
        return roll

    def strength(self, context: str, low: float, high: float) -> Roll:
        if not low <= high:
            raise ValueError("BAD_RANGE")
        value = low + self._random(context).random() * (high - low)
        roll = Roll(self.seed, context, (), (), f"{value:.6f}", round(value, 9))
        self.log.append(roll)
        return roll
