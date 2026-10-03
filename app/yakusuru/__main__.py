import sys


def main() -> int:
    # Headless helpers: `python -m yakusuru --doctor` prints an environment report.
    if "--doctor" in sys.argv:
        from .hardware import detect, format_report
        print(format_report(detect()))
        return 0
    from .gui.app import run
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
