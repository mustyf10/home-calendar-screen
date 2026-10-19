"""Transcription reads the WAV that extract_audio writes, without faster-whisper's PyAV decoder."""
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from faster_whisper.audio import decode_audio

from app.ingest import video


class ModelThatDecodesLikeFasterWhisper:
    """Takes audio the way WhisperModel.transcribe does: paths go through faster-whisper's decoder."""

    def __init__(self):
        self.samples = None

    def transcribe(self, audio, **kwargs):
        self.samples = audio if isinstance(audio, np.ndarray) else decode_audio(audio)
        return iter([SimpleNamespace(text=" Fry the onions. "), SimpleNamespace(text="Add the chicken.")]), None


def write_wav(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())


@pytest.fixture
def model(monkeypatch):
    fake = ModelThatDecodesLikeFasterWhisper()
    monkeypatch.setattr(video, "_whisper_model", fake)
    return fake


def test_transcribes_the_extracted_wav(tmp_path, model):
    audio = tmp_path / "audio.wav"
    tone = (np.sin(np.arange(16000) / 5) * 8000).astype(np.int16)
    write_wav(audio, tone)

    text = video.transcribe(audio)

    assert text == "Fry the onions. Add the chicken."
    assert model.samples.dtype == np.float32 and len(model.samples) == 16000
    assert np.allclose(model.samples, tone / 32768.0)


def test_rejects_audio_that_is_not_16khz_mono(tmp_path, model):
    audio = tmp_path / "stereo.wav"
    write_wav(audio, np.zeros(200, np.int16), rate=44100, channels=2)

    with pytest.raises(ValueError, match="16 kHz mono"):
        video.transcribe(audio)
