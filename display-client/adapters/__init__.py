"""Trusted local display adapters; importing a spec must not touch hardware."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from PIL import Image

POLL_INTERVAL = timedelta(minutes=5)


def validate_size(size: tuple[int, int]) -> None:
    if (type(size) is not tuple or len(size) != 2 or
            any(type(axis) is not int or not 1 <= axis <= 8192 for axis in size) or
            size[0] * size[1] > 16_777_216):
        raise ValueError("panel size must contain integer dimensions 1–8192 and at most 16777216 pixels")


@dataclass(frozen=True)
class PanelSpec:
    size: tuple[int, int]
    min_interval: timedelta
    maintenance_after: timedelta | None
    max_interval: timedelta | None

    def __post_init__(self) -> None:
        validate_size(self.size)
        if not isinstance(self.min_interval, timedelta) or self.min_interval < timedelta():
            raise ValueError("min_interval must be a nonnegative timedelta")
        if (self.maintenance_after is None) != (self.max_interval is None):
            raise ValueError("maintenance_after and max_interval must both be present or absent")
        if self.maintenance_after is not None:
            if (not isinstance(self.maintenance_after, timedelta) or
                    not isinstance(self.max_interval, timedelta) or
                    not self.min_interval <= self.maintenance_after < self.max_interval):
                raise ValueError("maintenance requires min_interval <= maintenance_after < max_interval timedeltas")
            if self.max_interval - self.maintenance_after < POLL_INTERVAL:
                raise ValueError("maintenance window must allow the five-minute poll interval")


class Adapter(Protocol):
    spec: PanelSpec

    def show(self, image: Image.Image) -> None: ...
