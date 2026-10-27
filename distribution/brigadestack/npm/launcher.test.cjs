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
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'brigadestack-contract-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const pkg = path.join(root, 'bridge');
  const dependency = path.join(pkg, 'node_modules', '@escoffier-labs', 'brigade');
  fs.mkdirSync(dependency, { recursive: true });
  const launcher = path.join(pkg, 'launcher.cjs');
  fs.copyFileSync(path.join(__dirname, 'launcher.cjs'), launcher);
  fs.writeFileSync(path.join(dependency, 'package.json'), JSON.stringify({
    name: '@escoffier-labs/brigade', version: '0.30.0', bin: { brigade: 'launcher.cjs' },
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
  assert.match(result.stderr, /brigadestack.*dependency|brigadestack.*install/i);
  assert.match(result.stderr, /Reinstall brigadestack with dependencies enabled/);
});

const posix = { skip: process.platform === 'win32' && 'POSIX signal and process group contract', timeout: 12000 };

test('missing execve reports the supported Node runtime instead of reinstall advice', posix, t => {
  const f = fixture(t, "throw new Error('dependency must not run');");
  const result = spawnSync(process.execPath, ['-e',
    'process.execve = undefined; require(process.argv[1]);', f.launcher,
  ], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /requires Node\.js \^22\.15\.0 \|\| >=23\.11\.0/);
  assert.doesNotMatch(result.stderr, /reinstall/i);
  assert.equal(result.stdout, '');
});

test('process replacement rejection reports its cause instead of reinstall advice', posix, t => {
  const f = fixture(t, "throw new Error('dependency must not run');");
  // execve exists but Node rejects replacing the process from a worker thread.
  const result = spawnSync(process.execPath, ['-e', `
    const { Worker } = require('node:worker_threads');
    new Worker(process.argv[1]).on('exit', code => { process.exitCode = code; });
  `, f.launcher], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /brigadestack: could not replace process: .*worker/i);
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
    assert.match(result.stderr, /brigadestack: could not spawn dependency:/);
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
