"""Trusted module entry for the bounded Claude hook subprocess."""

from .runtime import _hook_worker_main

if __name__ == "__main__":
    _hook_worker_main()
