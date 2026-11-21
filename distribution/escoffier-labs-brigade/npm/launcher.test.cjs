'use strict';
// Exercise the executable with an ordinary on-disk dependency, no launcher hooks.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');

function fixture(t, script) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'escoffier-brigade-contract-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const pkg = path.join(root, 'compat');
  const dependency = path.join(pkg, 'node_modules', 'brigadestack');
  fs.mkdirSync(dependency, { recursive: true });
  const launcher = path.join(pkg, 'launcher.cjs');
  fs.copyFileSync(path.join(__dirname, 'launcher.cjs'), launcher);
  fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({
    name: '@escoffier-labs/brigade', version: '0.32.0', dependencies: { brigadestack: '0.32.0' },
  }));
  fs.writeFileSync(path.join(dependency, 'package.json'), JSON.stringify({
    name: 'brigadestack', version: '0.32.0', bin: { brigade: 'launcher.cjs', brigadestack: 'launcher.cjs' },
  }));
  fs.writeFileSync(path.join(dependency, 'launcher.cjs'), script);
  return { root, launcher, dependency };
}

test('passes literal arguments, streams, environment, cwd and nonzero status without PATH', t => {
  const f = fixture(t, `
    const fs = require('node:fs');
    console.log(JSON.stringify({args: process.argv.slice(2), cwd: process.cwd(),
      input: fs.readFileSync(0, 'utf8'), value: process.env.BRIDGE_EXAMPLE}));
    console.error('dependency stderr');
    process.exitCode = 23;
  `);
  const args = ['two words', '"quoted"', '; touch forbidden', '$(touch forbidden)', '*', '', 'line\nbreak', 'café'];
  const result = spawnSync(process.execPath, [f.launcher, ...args], {
    cwd: f.root, input: 'stdin\nbytes\n', encoding: 'utf8',
    env: { ...process.env, PATH: '', BRIDGE_EXAMPLE: 'inherited' }, timeout: 5000,
  });
  assert.equal(result.status, 23, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    args, cwd: fs.realpathSync(f.root), input: 'stdin\nbytes\n', value: 'inherited',
  });
  assert.equal(result.stderr, 'dependency stderr\n');
  assert.equal(fs.existsSync(path.join(f.root, 'forbidden')), false);
});

test('missing dependency fails with an installation diagnostic', t => {
  const f = fixture(t, '');
  fs.rmSync(f.dependency, { recursive: true });
  const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /@escoffier-labs\/brigade.*could not find its brigadestack dependency/i);
  assert.match(result.stderr, /Reinstall @escoffier-labs\/brigade with dependencies enabled/);
});

test('a brigadestack found only through NODE_PATH is never run', t => {
  const f = fixture(t, '');
  fs.rmSync(f.dependency, { recursive: true });
  // A complete, correctly versioned decoy: the launcher must not look outside its own ancestors.
  const decoy = path.join(f.root, 'elsewhere', 'brigadestack');
  fs.mkdirSync(decoy, { recursive: true });
  fs.writeFileSync(path.join(decoy, 'package.json'), JSON.stringify({ name: 'brigadestack', version: '0.32.0' }));
  fs.writeFileSync(path.join(decoy, 'launcher.cjs'), `require('node:fs').writeFileSync(${JSON.stringify(path.join(f.root, 'decoy-ran'))}, 'ran');`);
  const result = spawnSync(process.execPath, [f.launcher], {
    encoding: 'utf8', timeout: 5000, env: { ...process.env, NODE_PATH: path.join(f.root, 'elsewhere') },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /could not find its brigadestack dependency/);
  assert.equal(fs.existsSync(path.join(f.root, 'decoy-ran')), false);
});

for (const [label, change, message] of [
  ['a different version', manifest => { manifest.version = '0.32.1'; }, /found brigadestack 0\.32\.1.*needs exactly 0\.32\.0/],
  ['a different package name', manifest => { manifest.name = 'other-package'; }, /found brigadestack/],
]) {
  test(`${label} of the dependency is refused before it runs`, t => {
    const f = fixture(t, `require('node:fs').writeFileSync(${JSON.stringify('PLACEHOLDER')}, 'ran');`);
    const ran = path.join(f.root, 'dependency-ran');
    fs.writeFileSync(path.join(f.dependency, 'launcher.cjs'), `require('node:fs').writeFileSync(${JSON.stringify(ran)}, 'ran');`);
    const file = path.join(f.dependency, 'package.json');
    const manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
    change(manifest);
    fs.writeFileSync(file, JSON.stringify(manifest));
    const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
    assert.equal(result.status, 1);
    assert.match(result.stderr, message);
    assert.equal(fs.existsSync(ran), false);
  });
}

test('a nearer wrong-version dependency is not skipped in favour of a farther correct one', t => {
  const f = fixture(t, '');
  const file = path.join(f.dependency, 'package.json');
  fs.writeFileSync(file, JSON.stringify({ name: 'brigadestack', version: '0.0.1' }));
  const farther = path.join(f.root, 'node_modules', 'brigadestack');
  fs.mkdirSync(farther, { recursive: true });
  fs.writeFileSync(path.join(farther, 'package.json'), JSON.stringify({ name: 'brigadestack', version: '0.32.0' }));
  fs.writeFileSync(path.join(farther, 'launcher.cjs'), '');
  const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /found brigadestack 0\.0\.1/);
});

test('a launcher that is a symlink out of the package root is refused', { skip: process.platform === 'win32' && 'symlink creation needs privileges' }, t => {
  const f = fixture(t, '');
  const outside = path.join(f.root, 'outside-launcher.cjs');
  fs.writeFileSync(outside, `require('node:fs').writeFileSync(${JSON.stringify(path.join(f.root, 'outside-ran'))}, 'ran');`);
  fs.rmSync(path.join(f.dependency, 'launcher.cjs'));
  fs.symlinkSync(outside, path.join(f.dependency, 'launcher.cjs'));
  const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /not a regular file inside that package/);
  assert.equal(fs.existsSync(path.join(f.root, 'outside-ran')), false);
});

test('a launcher that is a directory is refused', t => {
  const f = fixture(t, '');
  fs.rmSync(path.join(f.dependency, 'launcher.cjs'));
  fs.mkdirSync(path.join(f.dependency, 'launcher.cjs'));
  const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /not a regular file inside that package/);
});

test('a symlinked brigadestack directory is followed and checked at its real location', { skip: process.platform === 'win32' && 'symlink creation needs privileges' }, t => {
  const f = fixture(t, "console.log('real');");
  const real = path.join(f.root, 'real-brigadestack');
  fs.renameSync(f.dependency, real);
  fs.symlinkSync(real, f.dependency, 'dir');
  const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), 'real');
});

const posix = { skip: process.platform === 'win32' && 'POSIX signal and process group contract', timeout: 12000 };

// Node 22.0 to 22.14 has no process.execve, and `npm update -g` ignores engines, so
// the launcher must still work there: it falls back to a child process.
const noExecve = ['-e', 'process.execve = undefined; require(process.argv[1]);'];

test('without process.execve it still passes literal arguments, streams, environment and status', posix, t => {
  const f = fixture(t, `
    const fs = require('node:fs');
    console.log(JSON.stringify({args: process.argv.slice(2), cwd: process.cwd(),
      input: fs.readFileSync(0, 'utf8'), value: process.env.BRIDGE_EXAMPLE}));
    console.error('dependency stderr');
    process.exitCode = 23;
  `);
  const args = ['two words', '"quoted"', '$(touch forbidden)', '', 'café'];
  const result = spawnSync(process.execPath, [...noExecve, f.launcher, ...args], {
    cwd: f.root, input: 'stdin\nbytes\n', encoding: 'utf8',
    env: { ...process.env, PATH: '', BRIDGE_EXAMPLE: 'inherited' }, timeout: 5000,
  });
  assert.equal(result.status, 23, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    args, cwd: fs.realpathSync(f.root), input: 'stdin\nbytes\n', value: 'inherited',
  });
  assert.equal(result.stderr, 'dependency stderr\n');
  assert.equal(fs.existsSync(path.join(f.root, 'forbidden')), false);
});

test('without process.execve a signal death of the dependency is reported as that signal', posix, t => {
  const f = fixture(t, "process.kill(process.pid, 'SIGTERM');");
  const result = spawnSync(process.execPath, [...noExecve, f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, null, result.stderr);
  assert.equal(result.signal, 'SIGTERM');
});

test('without process.execve a dependency that ignores SIGTERM is killed after the escalation delay', { ...posix, timeout: 15000 }, async t => {
  const f = fixture(t, `
    process.on('SIGTERM', () => {});
    console.log(process.pid);
    setInterval(() => {}, 1000);
  `);
  const child = spawn(process.execPath, [...noExecve, f.launcher], { detached: true, stdio: ['ignore', 'pipe', 'pipe'], cwd: f.root });
  let dependencyPid;
  t.after(() => { for (const pid of [dependencyPid, child.pid]) { if (pid) { try { process.kill(-pid, 'SIGKILL'); } catch {} } } });
  let stdout = '';
  child.stdout.on('data', data => { stdout += data; });
  const closed = once(child, 'close');
  await once(child.stdout, 'data');
  dependencyPid = Number(stdout.trim());
  const started = Date.now();
  process.kill(child.pid, 'SIGTERM');
  const [code, signal] = await closed;
  assert.deepEqual([code, signal], [137, null]);
  assert.ok(Date.now() - started >= 1500, 'the signal is forwarded after the grace period first');
  assert.throws(() => process.kill(dependencyPid, 0), { code: 'ESRCH' });
});

test('without process.execve a second signal kills a dependency that ignores the first immediately', { ...posix, timeout: 15000 }, async t => {
  const f = fixture(t, `
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => {});
    console.log(process.pid);
    setInterval(() => {}, 1000);
  `);
  const child = spawn(process.execPath, [...noExecve, f.launcher], { detached: true, stdio: ['ignore', 'pipe', 'pipe'], cwd: f.root });
  let dependencyPid;
  t.after(() => { for (const pid of [dependencyPid, child.pid]) { if (pid) { try { process.kill(-pid, 'SIGKILL'); } catch {} } } });
  let stdout = '';
  child.stdout.on('data', data => { stdout += data; });
  const closed = once(child, 'close');
  await once(child.stdout, 'data');
  dependencyPid = Number(stdout.trim());
  const started = Date.now();
  process.kill(child.pid, 'SIGTERM');
  await new Promise(resolve => setTimeout(resolve, 200));
  process.kill(child.pid, 'SIGTERM');
  const [code, signal] = await closed;
  assert.deepEqual([code, signal], [137, null]);
  assert.ok(Date.now() - started < 1400, 'the second signal must not wait for the grace period');
});

for (const delivery of ['pid', 'group']) {
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    test(`without process.execve ${delivery} ${signal} reaches dependency once and preserves handled status`, posix, async t => {
      const f = fixture(t, `
        const signals = [];
        for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => {
          signals.push(signal);
          if (signals.length === 1) setTimeout(() => {
            console.log(JSON.stringify(signals)); process.exit(23);
          }, 300);
        });
        console.log(process.pid);
        setInterval(() => {}, 1000);
      `);
      const child = spawn(process.execPath, [...noExecve, f.launcher], {
        detached: true, stdio: ['ignore', 'pipe', 'pipe'], cwd: f.root,
      });
      let dependencyPid;
      t.after(() => {
        for (const pid of [dependencyPid, child.pid]) {
          if (pid) { try { process.kill(-pid, 'SIGKILL'); } catch {} }
        }
      });
      let stdout = '';
      let stderr = '';
      child.stdout.on('data', data => { stdout += data; });
      child.stderr.on('data', data => { stderr += data; });
      const closed = once(child, 'close');
      await Promise.race([
        once(child.stdout, 'data'),
        closed.then(() => { throw new Error(`exited before ready: ${stderr}`); }),
      ]);
      dependencyPid = Number(stdout.trim());
      assert.ok(dependencyPid > 0, stdout);
      // A PID signal reaches only the launcher, which forwards it after its grace period.
      // A group signal reaches the dependency too, so the launcher must not repeat it.
      process.kill(delivery === 'group' ? -child.pid : child.pid, signal);
      assert.deepEqual(await closed, [23, null], stderr);
      assert.deepEqual(JSON.parse(stdout.trim().split('\n')[1]), [signal]);
    });
  }
}

test('process replacement rejection reports its cause instead of reinstall advice', posix, t => {
  const f = fixture(t, "throw new Error('dependency must not run');");
  // execve exists but Node rejects replacing the process from a worker thread.
  const result = spawnSync(process.execPath, ['-e', `
    const { Worker } = require('node:worker_threads');
    new Worker(process.argv[1]).on('exit', code => { process.exitCode = code; });
  `, f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /@escoffier-labs\/brigade: could not replace process: .*worker/i);
  assert.doesNotMatch(result.stderr, /reinstall/i);
  assert.equal(result.stdout, '');
});

for (const executable of ['missing', 'invalid']) {
  test(`Windows ${executable} executable reports a spawn error instead of reinstall advice`, t => {
    const f = fixture(t, "throw new Error('dependency must not run');");
    // Exercise the Windows branch with real spawn errors on any development host.
    const execPath = executable === 'missing' ? path.join(f.root, 'missing-node') : null;
    const result = spawnSync(process.execPath, ['-e', `
      Object.defineProperty(process, 'platform', { value: 'win32' });
      process.execPath = ${JSON.stringify(execPath)};
      require(process.argv[1]);
    `, f.launcher], { encoding: 'utf8', timeout: 5000 });
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /@escoffier-labs\/brigade: could not spawn dependency:/);
    assert.match(result.stderr, executable === 'missing' ? /ENOENT/ : /string.*null/is);
    assert.doesNotMatch(result.stderr, /reinstall/i);
    assert.equal(result.stdout, '');
  });
}

for (const delivery of ['pid', 'group']) {
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    test(`${delivery} ${signal} reaches dependency once through slow cleanup and preserves handled status`, posix, async t => {
      const f = fixture(t, `
        const signals = [];
        for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => {
          signals.push(signal);
          if (signals.length === 1) setTimeout(() => {
            console.log(JSON.stringify(signals)); process.exit(23);
          }, 1800);
        });
        console.log(process.pid);
        setInterval(() => {}, 1000);
      `);
      const child = spawn(process.execPath, [f.launcher], {
        detached: true, stdio: ['ignore', 'pipe', 'pipe'], cwd: f.root,
      });
      let dependencyPid;
      t.after(() => {
        for (const pid of [dependencyPid, child.pid]) {
          if (pid) { try { process.kill(-pid, 'SIGKILL'); } catch {} }
        }
      });
      let stdout = '';
      let stderr = '';
      child.stdout.on('data', data => { stdout += data; });
      child.stderr.on('data', data => { stderr += data; });
      const closed = once(child, 'close');
      await Promise.race([
        once(child.stdout, 'data'),
        closed.then(() => { throw new Error(`exited before ready: ${stderr}`); }),
      ]);
      dependencyPid = Number(stdout.trim());
      assert.ok(dependencyPid > 0, stdout);
      process.kill(delivery === 'group' ? -child.pid : child.pid, signal);
      assert.deepEqual(await closed, [23, null], stderr);
      assert.deepEqual(JSON.parse(stdout.trim().split('\n')[1]), [signal]);
    });
  }
}

test('unhandled dependency signal is preserved by process replacement', posix, t => {
  const f = fixture(t, "process.kill(process.pid, 'SIGTERM');");
  const result = spawnSync(process.execPath, [f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, null, result.stderr);
  assert.equal(result.signal, 'SIGTERM');
});
