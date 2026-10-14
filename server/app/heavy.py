"""Serialises memory-heavy work (Whisper transcription, Chromium renders) on the Pi 4."""
import asyncio

heavy_lock = asyncio.Lock()
