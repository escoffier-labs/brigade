from .tricky import *
from . import tricky

try:
    from fastjson import dumps
except ImportError:
    from json import dumps


def consume():
    decorated(1)
    tricky.outer()
    return dumps(helper(2))


class Service:
    def run(self):
        return consume()


consume()
