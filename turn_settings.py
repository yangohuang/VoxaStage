"""Validated timing knobs for local, VAD-segmented speech."""
from dataclasses import asdict, dataclass
import math
import os


@dataclass(frozen=True)
class TurnSettings:
    vad_stop_secs: float = .6
    turn_wait_secs: float = .5

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        values = {}
        for field, key, default in (
            ('vad_stop_secs', 'PIPECAT_VAD_STOP_SECS', .6),
            ('turn_wait_secs', 'PIPECAT_TURN_WAIT_SECS', .5),
        ):
            value = float(env.get(key, default))
            if not math.isfinite(value) or not .1 <= value <= 2:
                raise ValueError(f'{key} must be finite and between 0.1 and 2 seconds')
            values[field] = value
        return cls(**values)

    def as_dict(self):
        return asdict(self)
