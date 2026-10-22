"""Waveshare 7.5-inch V2 normal full-refresh adapter."""
from . import DEFAULT_PANEL_SPEC


class Waveshare7in5V2:
    spec = DEFAULT_PANEL_SPEC

    def show(self, image) -> None:
        from waveshare_epd import epd7in5_V2

        epd = epd7in5_V2.EPD()
        try:
            epd.init()
            epd.display(epd.getbuffer(image))
        finally:
            epd.sleep()


def create() -> Waveshare7in5V2:
    return Waveshare7in5V2()
