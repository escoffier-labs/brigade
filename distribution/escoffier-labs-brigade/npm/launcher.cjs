#!/usr/bin/env node
'use strict';
const { spawn } = require('node:child_process');
const { constants } = require('node:os');

let launcher;
try {
  launcher = require.resolve('brigadestack/launcher.cjs');
} catch (error) {
  console.error(`@escoffier-labs/brigade: could not resolve its brigadestack dependency. Reinstall @escoffier-labs/brigade with dependencies enabled, or install brigadestack directly. ${error.message}`);
  process.exitCode = 1;
  return;
}

// process.execve (Node 22.15 and 23.11 or newer) replaces this launcher with the real one,
// preserving its PID, process group and controlling terminal. npm ignores engines on
// `npm update -g`, so older Node 22 must still work: it falls back to a child process.
if (process.platform !== 'win32' && typeof process.execve === 'function') {
  try {
    process.execve(process.execPath, [process.execPath, launcher, ...process.argv.slice(2)], process.env);
  } catch (error) {
    console.error(`@escoffier-labs/brigade: could not replace process: ${error.message}`);
    process.exitCode = 1;
  }
} else {
  try {
    const child = spawn(process.execPath, [launcher, ...process.argv.slice(2)], {
      stdio: 'inherit', shell: false,
    });
    const handlers = new Map();
    let forwarding;
    let receivedSignal = null;
    for (const signal of ['SIGINT', 'SIGBREAK', 'SIGTERM', 'SIGHUP']) {
      const handler = () => {
        if (process.platform === 'win32') {
          // Attached console events already reach the dependency. Only a direct
          // SIGTERM needs forwarding from this launcher to its child.
          if (signal !== 'SIGTERM') return;
          if (child.exitCode === null && child.signalCode === null) child.kill(signal);
          return;
        }
        // POSIX group delivery (Ctrl-C, terminal hangup) also reaches the child, so give it
        // time to exit before forwarding a signal that only this PID received.
        if (receivedSignal !== null) return;
        receivedSignal = signal;
        forwarding = setTimeout(() => {
          if (child.exitCode === null && child.signalCode === null) child.kill(signal);
        }, 1500);
      };
      if (signal === 'SIGBREAK' && process.platform !== 'win32') continue;
      handlers.set(signal, handler);
      process.on(signal, handler);
    }
    function cleanup() {
      clearTimeout(forwarding);
      for (const [signal, handler] of handlers) process.removeListener(signal, handler);
    }
    child.once('error', error => {
      cleanup();
      console.error(`@escoffier-labs/brigade: could not spawn dependency: ${error.message}`);
      process.exitCode = 1;
    });
    child.once('exit', (code, signal) => {
      cleanup();
      if (code !== null) {
        process.exitCode = code;
      } else {
        process.exitCode = 128 + (constants.signals[signal] || 1);
        // Report the same signal death to the caller, as the dependency would have.
        if (process.platform !== 'win32' && ['SIGINT', 'SIGTERM', 'SIGHUP'].includes(signal)) process.kill(process.pid, signal);
      }
    });
  } catch (error) {
    console.error(`@escoffier-labs/brigade: could not spawn dependency: ${error.message}`);
    process.exitCode = 1;
  }
}
