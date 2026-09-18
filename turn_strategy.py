"""The local segmented ASR explicitly signals completion; don't guess from P99."""
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy


class LocalTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    async def _maybe_trigger_user_turn_stopped(self):
        # A resumed utterance invalidates the previous finalized transcript.
        # The generic strategy otherwise permits old text after the STT timer.
        if not self._transcript_finalized:
            return
        await super()._maybe_trigger_user_turn_stopped()
