"""Trusted module entry for the bounded Stop capture (S24)."""

from .memory_hook_hub import _capture_worker_main

if __name__ == "__main__":
    _capture_worker_main()
