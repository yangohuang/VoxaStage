import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from flashhead_worker import AudioChunker


def test_audio_chunker_keeps_only_real_pcm_frame_count_at_tail():
    """Contract test, not a FlashHead model test."""
    chunker = AudioChunker()
    chunker.feed(b"\x01\x00" * 961)

    assert chunker.total_input_samples == 961
    assert chunker.expected_frame_count == 2
    assert list(chunker.finish_model_chunks())


def test_audio_chunker_rejects_more_than_thirty_seconds():
    """Contract test, not a FlashHead model test."""
    chunker = AudioChunker()
    chunker.feed(b"\x00\x00" * (30 * 24_000))

    try:
        chunker.feed(b"\x00\x00")
    except ValueError as exc:
        assert "30 seconds" in str(exc)
    else:
        raise AssertionError("audio over the configured bound was accepted")
