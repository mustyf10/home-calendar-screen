"""Video platforms: yt-dlp download, ffmpeg audio/keyframes, local Whisper transcription.

yt_dlp and faster_whisper are imported lazily so the app imports without them configured.
"""
import logging
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MAX_DURATION_S = 900
TRANSCRIPT_LIMIT = 15_000
_whisper_model = None


@dataclass
class VideoData:
    caption: str
    canonical_url: str | None
    thumbnail_url: str | None
    media_path: Path | None


def _skip_long(info: dict, incomplete: bool = False) -> str | None:
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_S:
        return f"Video longer than {MAX_DURATION_S // 60} minutes; not downloading"
    return None


def fetch_video(url: str, tmp: Path) -> VideoData:
    """Raises yt_dlp.utils.DownloadError when the platform refuses (e.g. Instagram logged out)."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "worst[acodec!=none][vcodec!=none]/worst",
        "outtmpl": str(tmp / "media.%(ext)s"),
        "match_filter": _skip_long,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    media = next((p for p in sorted(tmp.glob("media.*")) if p.suffix not in (".part", ".ytdl")), None)
    return VideoData(
        caption=(info.get("description") or info.get("title") or "").strip(),
        canonical_url=info.get("webpage_url"),
        thumbnail_url=info.get("thumbnail"),
        media_path=media,
    )


def _run(args: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, timeout=timeout)


def extract_audio(media: Path, tmp: Path) -> Path:
    """16 kHz mono 16-bit WAV, the format transcribe() reads."""
    audio = tmp / "audio.wav"
    _run(["ffmpeg", "-nostdin", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", str(audio)])
    return audio


def _samples(audio: Path) -> np.ndarray:
    """Float samples in [-1, 1] from extract_audio's WAV.

    Handing faster-whisper samples rather than a path skips its PyAV decoder, which
    faster-whisper 1.2.1 calls with an argument PyAV 19 no longer accepts.
    """
    with wave.open(str(audio)) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise ValueError(f"expected 16 kHz mono 16-bit audio, got {audio.name}")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0


def transcribe(audio: Path) -> str:
    """Caller must hold app.heavy.heavy_lock."""
    global _whisper_model
    samples = _samples(audio)
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(
            "base", device="cpu", compute_type="int8", download_root="/opt/models", cpu_threads=4
        )
    segments, _info = _whisper_model.transcribe(samples, beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()[:TRANSCRIPT_LIMIT]


def duration_seconds(media: Path) -> float:
    out = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
        timeout=60,
    )
    return float(out.stdout.decode().strip())


def keyframes(media: Path, dest_dir: Path) -> list[Path]:
    """4 frames at 20/40/60/80% of the duration, written as dest_dir/frame-N.jpg."""
    duration = duration_seconds(media)
    dest_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for n, pct in enumerate((0.2, 0.4, 0.6, 0.8), start=1):
        out = dest_dir / f"frame-{n}.jpg"
        _run(
            ["ffmpeg", "-nostdin", "-y", "-ss", f"{duration * pct:.2f}", "-i", str(media),
             "-frames:v", "1", "-vf", "scale=768:-2", str(out)],
            timeout=120,
        )
        if out.exists():
            frames.append(out)
    return frames
