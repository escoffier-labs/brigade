"""Trusted module entry for bounded session-start memory recall."""

from .memory_hooks import _recall_worker_main

if __name__ == "__main__":
    _recall_worker_main()
