from pkg.commands import lint
from .sub.sibling import *


def tag(name):
    return lambda fn: fn


@tag("cli")
def cli():
    return lint()


if __name__ == "__main__":
    cli()
    func()
