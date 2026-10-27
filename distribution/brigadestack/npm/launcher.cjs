#!/usr/bin/env node
'use strict';
const { spawn } = require('node:child_process');
const { constants } = require('node:os');

let launcher;
try {
  launcher = require.resolve('@escoffier-labs/brigade/launcher.cjs');
} catch (error) {
  console.error(`brigadestack: could not resolve dependency. Reinstall brigadestack with dependencies enabled. ${error.message}`);
  process.exitCode = 1;
  return;
}

if (process.platform !== 'win32') {
  if (typeof process.execve !== 'function') {
    console.error(`brigadestack: process replacement requires Node.js ^22.15.0 || >=23.11.0 with process.execve available (running ${process.version}).`);
    process.exitCode = 1;
  } else {
    try {
      // Replace the bridge, preserving its PID, process group and controlling tty.
      process.execve(process.execPath, [process.execPath, launcher, ...process.argv.slice(2)], process.env);
    } catch (error) {
      console.error(`brigadestack: could not replace process: ${error.message}`);
      process.exitCode = 1;
    }
  }
} else {
  try {
    const child = spawn(process.execPath, [launcher, ...process.argv.slice(2)], {
      stdio: 'inherit', shell: false,
    });
    const handlers = new Map();
    for (const signal of ['SIGINT', 'SIGBREAK', 'SIGTERM', 'SIGHUP']) {
      const handler = () => {
        // Attached console events already reach the dependency. Only a direct
        // SIGTERM needs forwarding from the bridge to its child.
        if (signal !== 'SIGTERM') return;
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
      console.error(`brigadestack: could not spawn dependency: ${error.message}`);
      process.exitCode = 1;
    });
    child.once('exit', (code, signal) => {
      cleanup();
      process.exitCode = code !== null ? code : 128 + (constants.signals[signal] || 1);
    });
  } catch (error) {
    console.error(`brigadestack: could not spawn dependency: ${error.message}`);
    process.exitCode = 1;
  }
}
