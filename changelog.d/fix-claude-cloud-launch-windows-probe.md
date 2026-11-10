- On Windows, `brigade run cloud launch --provider claude-cloud` and every other command that
  resolves the user data, cache or config directory no longer starts a `ver` child process when
  the system's WMI query fails. Brigade now detects Windows from the interpreter.
