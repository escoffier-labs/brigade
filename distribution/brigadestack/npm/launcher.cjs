#!/usr/bin/env node
'use strict';
const { spawn } = require('node:child_process');
const { constants } = require('node:os');

try {
  const launcher = require.resolve('@escoffier-labs/brigade/launcher.cjs');
  const windows = process.platform === 'win32';
  const child = spawn(process.execPath, [launcher, ...process.argv.slice(2)], {
    stdio: 'inherit', shell: false,
    // POSIX terminal group signals reach this bridge only. Forward once to the
    // dependency, which owns cancellation of its managed runtime and children.
    detached: !windows,
  });
  const handlers = new Map();
  for (const signal of windows ? ['SIGINT', 'SIGBREAK', 'SIGTERM', 'SIGHUP'] : ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    const handler = () => {
      // Attached Windows console events already reach the dependency.
      if (windows && signal !== 'SIGTERM') return;
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    };
    handlers.set(signal, handler);
    process.on(signal, handler);
  }
  function cleanup() {
    for (const [signal, handler] of handlers) process.removeListener(signal, handler);
  }
  child.once('error', error => {
    cleanup();
    console.error(`brigadestack: could not launch dependency: ${error.message}`);
    process.exitCode = 1;
  });
  child.once('exit', (code, signal) => {
    cleanup();
    if (code !== null) process.exitCode = code;
    else {
      process.exitCode = 128 + (constants.signals[signal] || 1);
      if (!windows) process.kill(process.pid, signal);
    }
  });
} catch (error) {
  console.error(`brigadestack: could not resolve dependency. Reinstall brigadestack with dependencies enabled. ${error.message}`);
  process.exitCode = 1;
}
