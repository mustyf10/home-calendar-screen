"""Run one council scraper, keeping council chatter on stderr and JSON on stdout."""
import contextlib
import sys


def main(argv: list[str]) -> None:
    from uk_bin_collection.uk_bin_collection.collect_data import UKBinCollectionApp

    out = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        app = UKBinCollectionApp()
        app.set_args(argv)
        result = app.run()
    out.write(result)


if __name__ == "__main__":
    main(sys.argv[1:])
