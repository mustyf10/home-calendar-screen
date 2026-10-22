"""Waveshare 7.5-inch V2 normal full-refresh adapter."""
from datetime import timedelta

from . import PanelSpec


class Waveshare7in5V2:
    spec = PanelSpec((800, 480), timedelta(seconds=180),
                     timedelta(hours=23, minutes=45), timedelta(hours=24))

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
