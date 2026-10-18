"""Private process-local distribution identity set by an entry-point bootstrap."""

_name: str | None = None


def is_npm() -> bool:
    return _name == "npm"
