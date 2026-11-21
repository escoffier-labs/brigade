#!/usr/bin/env node
'use strict';
const { spawn } = require('node:child_process');
const { constants } = require('node:os');

const fs = require('node:fs');
const path = require('node:path');

// Run exactly the brigadestack this package was published with. Node's own resolution could
// pick another brigadestack from NODE_PATH or a distant ancestor node_modules, so walk up from
// this package's directory by hand (nearest wins, like Node, and NODE_PATH is never consulted),
// then require the exact version and a regular launcher file inside that package.
function fail(message) {
  console.error(`@escoffier-labs/brigade: ${message}`);
  process.exitCode = 1;
}
function resolveDependency() {
  const own = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8'));
  const expected = own.dependencies && own.dependencies.brigadestack;
  if (own.name !== '@escoffier-labs/brigade' || own.version !== expected) {
    throw new Error('this package must depend on exactly its own version of brigadestack. Reinstall @escoffier-labs/brigade.');
  }
  let root = null;
  for (let dir = __dirname; ; dir = path.dirname(dir)) {
    const candidate = path.join(dir, 'node_modules', 'brigadestack');
    if (fs.existsSync(path.join(candidate, 'package.json'))) { root = candidate; break; }
    if (path.dirname(dir) === dir) break;
  }
  if (root === null) {
    throw new Error('could not find its brigadestack dependency. Reinstall @escoffier-labs/brigade with dependencies enabled, or install brigadestack directly.');
  }
  const realRoot = fs.realpathSync(root);
  const manifest = JSON.parse(fs.readFileSync(path.join(realRoot, 'package.json'), 'utf8'));
  if (manifest.name !== 'brigadestack' || manifest.version !== expected) {
    throw new Error(`found brigadestack ${manifest.version} at ${realRoot}, but this package needs exactly ${expected}. Reinstall @escoffier-labs/brigade.`);
  }
  const launcherPath = path.join(realRoot, 'launcher.cjs');
  const entry = fs.lstatSync(launcherPath);
  if (!entry.isFile() || entry.isSymbolicLink() || fs.realpathSync(launcherPath) !== launcherPath) {
    throw new Error(`the launcher of brigadestack at ${realRoot} is not a regular file inside that package. Reinstall brigadestack.`);
  }
  return launcherPath;
}

let launcher;
try {
  launcher = resolveDependency();
} catch (error) {
  fail(`${error.message}`);
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
    let escalation;
    let receivedSignal = null;
    const alive = () => child.exitCode === null && child.signalCode === null;
    // A child that ignores the forwarded signal must not keep this launcher alive forever.
    const force = () => { if (alive()) child.kill('SIGKILL'); };
    for (const signal of ['SIGINT', 'SIGBREAK', 'SIGTERM', 'SIGHUP']) {
      const handler = () => {
        if (process.platform === 'win32') {
          // Attached console events already reach the dependency. Only a direct
          // SIGTERM needs forwarding from this launcher to its child.
          if (signal !== 'SIGTERM') return;
          if (receivedSignal !== null) { force(); return; }
          receivedSignal = signal;
          if (alive()) child.kill(signal);
          escalation = setTimeout(force, 5000);
          return;
        }
        // A second signal means the caller is insisting: stop waiting for the child's cleanup.
        if (receivedSignal !== null) { force(); return; }
        receivedSignal = signal;
        // POSIX group delivery (Ctrl-C, terminal hangup) also reaches the child, so give it
        // time to exit before forwarding a signal that only this PID received, then escalate.
        forwarding = setTimeout(() => { if (alive()) child.kill(signal); }, 1500);
        escalation = setTimeout(force, 1500 + 5000);
      };
      if (signal === 'SIGBREAK' && process.platform !== 'win32') continue;
      handlers.set(signal, handler);
      process.on(signal, handler);
    }
    function cleanup() {
      clearTimeout(forwarding);
      clearTimeout(escalation);
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
