"""Enable `python -m brigade`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
