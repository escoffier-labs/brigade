"""Constructs the Python extractor must not drop."""

import functools
import os
from collections import *
from typing import TYPE_CHECKING

try:
    import ujson as json
except ImportError:
    import json

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["decorated", "outer", "fetch"]


def deco(arg):
    return passthrough


def passthrough(fn):
    return fn


def helper(x):
    return len(str(x))


@deco("a")
@functools.lru_cache(maxsize=None)
def decorated(x):
    return helper(x)


def outer():
    def inner():
        def innermost():
            return helper(1)

        return innermost()

    return inner()


async def fetch(session):
    data = await session.get("url")
    return await parse(data)


async def parse(data):
    async def nested():
        return helper(data)

    return await nested()


square = lambda n: helper(n)
callbacks = [lambda: helper(1), lambda: deco(2)]


def uses_lambda():
    return sorted([3, 1], key=lambda v: helper(v))


class Base:
    registry = build_registry()

    def method(self):
        return self.other()

    def other(self):
        return helper(1)

    @staticmethod
    def static():
        return helper(2)

    @classmethod
    def make(cls):
        return cls()

    @property
    def prop(self):
        return helper(3)

    class Inner:
        def deep(self):
            return Base().method()


def build_registry():
    return json.loads("{}")


class Child(Base):
    def method(self):
        return super().method()


def multi_line():
    return helper(
        1
    ).bit_length()


def chained():
    return os.path.join("a", "b").strip()


def conditional_def():
    if os.name == "nt":

        def impl():
            return helper(1)

    else:

        def impl():
            return helper(2)

    return impl()


def with_import():
    from pkg.consumer import consume

    return consume()


registry = build_registry()

if __name__ == "__main__":
    outer()
