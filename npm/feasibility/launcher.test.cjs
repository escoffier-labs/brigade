'use strict';
// These subprocess stand-ins prove transport only. acceptance.cjs uses real uv/Python.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const source = process.env.BRIGADE_NPM_TRANSPORT_SOURCE || path.join(__dirname, 'launcher.cjs');

function fixture(t, mode = 'echo') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'brigade-transport-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const pkg = path.join(root, 'launcher');
  fs.mkdirSync(pkg);
  fs.copyFileSync(source, path.join(pkg, 'launcher.cjs'));
  const companion = path.join(root, 'node_modules', '@example', 'uv');
  fs.mkdirSync(companion, { recursive: true });
  const script = `#!${process.execPath}\n` + `
const fs = require('node:fs');
const path = require('node:path');
if(process.argv[4]?.includes('sysconfig.get_path')) {
 const prefix=path.dirname(path.dirname(__filename));const purelib=path.join(prefix,'lib','site-packages');fs.mkdirSync(purelib,{recursive:true});
 if(process.env.TRANSPORT_IDENTITY_READY) {fs.writeFileSync(process.env.TRANSPORT_IDENTITY_READY,'ready');setInterval(()=>{},1000);}
 else {console.log(JSON.stringify({prefix,purelib:${JSON.stringify(mode)}==='identity-invalid'?path.dirname(prefix):purelib}));process.exit(0);}
} else if (['cleanup-wait','bootstrap-wait'].includes(${JSON.stringify(mode)})) {
  const signals=[];
  for(const signal of ['SIGINT','SIGTERM']) process.on(signal, () => {
    signals.push(signal);
    if(signals.length!==1) return;
    fs.writeFileSync(process.env.TRANSPORT_STARTED,'started');
    setTimeout(()=>{fs.writeFileSync(process.env.TRANSPORT_DONE,JSON.stringify(signals));process.exit(23);},1800);
  });
  fs.writeFileSync(process.env.TRANSPORT_READY,'ready');
  setInterval(() => {}, 1000);
} else if (['wait','slow-wait'].includes(${JSON.stringify(mode)})) {
  let signals=0;for(const signal of ['SIGINT','SIGTERM']) process.on(signal, () => { signals++;setTimeout(()=>{fs.writeFileSync(process.env.TRANSPORT_DONE,signals===1?'terminated':'duplicate');process.exit(0);},${mode === 'slow-wait' ? 800 : 0}); });
  console.log(JSON.stringify({pid:process.pid}));
  setInterval(() => {}, 1000);
} else if (${JSON.stringify(mode)} === 'other-signal') {
  process.kill(process.pid,'SIGUSR2');
} else {
  console.log(JSON.stringify({args: process.argv.slice(2), cwd: process.cwd(), env: Object.fromEntries(['PATH','UV_CACHE_DIR','UV_TOOL_DIR','UV_TOOL_BIN_DIR','UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','UV_INDEX_URL','UV_PYTHON','UV_CONFIG_FILE','UV_NO_CACHE','UV_OFFLINE','UV_PYTHON_DOWNLOADS','UV_NATIVE_TLS','UV_HTTP_TIMEOUT','BRIGADE_EXAMPLE','BRIGADE_NO_UPDATE_CHECK','UV_MANAGED_PYTHON','UV_PYTHON_CPYTHON_BUILD','PYTHONPATH','VIRTUAL_ENV'].filter(name => Object.hasOwn(process.env,name)).map(name => [name,process.env[name]])), stdin: fs.readFileSync(0, 'utf8')}));
  console.error('transport stderr');
  process.exit(${mode === 'exit' ? 23 : 0});
}
`;
  const installer=`#!${process.execPath}\nconst fs=require('node:fs'); const path=require('node:path');
if(process.argv.includes('run') || ${JSON.stringify(mode)}==='bootstrap-wait') {eval(${JSON.stringify(script.replace(/^#![^\n]*\n/,''))});} else {
const bin=path.join(process.env.UV_TOOL_DIR,'brigade-cli','bin'); fs.mkdirSync(bin,{recursive:true});
if(process.env.TRANSPORT_REDIRECT_PYTHON) {const selected=process.env[process.env.TRANSPORT_REDIRECT_PYTHON];fs.mkdirSync(path.dirname(selected),{recursive:true});fs.symlinkSync(process.env.TRANSPORT_OUTSIDE,selected,'dir');}
fs.writeFileSync(path.join(bin,'python'),${JSON.stringify(script)},{mode:0o755});
fs.appendFileSync(path.join(process.env.BRIGADE_NPM_CACHE,'install-count'),'1');
fs.writeFileSync(path.join(process.env.BRIGADE_NPM_CACHE,'bootstrap.json'),JSON.stringify({args:process.argv.slice(2),env:process.env}));
console.error('provisioning stderr');
}
`;
  fs.writeFileSync(path.join(companion, 'uv'), installer, { mode: 0o755 });
  fs.writeFileSync(path.join(companion, 'package.json'), JSON.stringify({name:'@example/uv',version:'0.29.0-dev.1',main:'manifest.json'}));
  const sha = crypto.createHash('sha256').update(installer).digest('hex');
  fs.writeFileSync(path.join(companion, 'manifest.json'), JSON.stringify({version:'0.29.0-dev.1',uvVersion:'0.12.23',executable:'uv',sha256:sha}));
  fs.mkdirSync(path.join(pkg,'wheel'));
  const wheel=Buffer.from('transport payload');fs.writeFileSync(path.join(pkg,'wheel','brigade_cli-0.29.0.dev1-py3-none-any.whl'),wheel);
  const payload={wheel:'brigade_cli-0.29.0.dev1-py3-none-any.whl',version:'0.29.0.dev1',distribution:'brigade-cli',sha256:crypto.createHash('sha256').update(wheel).digest('hex'),bytes:wheel.length,sourceCommit:'a'.repeat(40),sourceTree:'dirty'};
  fs.writeFileSync(path.join(pkg, 'runtime.json'), JSON.stringify({payload,payloadVersion:'0.29.0.dev1',version:'0.29.0-dev.1',uvVersion:'0.12.23',pythonVersion:'3.12.12',pythonBuild:'20260101',platforms:{[`${process.platform}-${process.arch}`]:{package:'@example/uv',executable:'uv',binarySha256:sha}}}));
  return {root,pkg,companion, launcher:path.join(pkg,'launcher.cjs')};
}
function run(f,args=[],env={}) {
  return spawnSync(process.execPath,[f.launcher,...args],{cwd:f.root,input:'input bytes\n',encoding:'utf8',env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state'),...env}});
}
const posix = {skip:process.platform === 'win32' ? 'POSIX executable transport fixture; real Windows acceptance is separate' : false};

test('missing platform package explains optional dependency installation', t => {
  const f=fixture(t); fs.rmSync(f.companion,{recursive:true});
  const r=run(f); assert.equal(r.status,1); assert.match(r.stderr,/--omit=optional/);
});
test('corrupt companion is rejected before it executes', t => {
  const f=fixture(t); fs.appendFileSync(path.join(f.companion,'uv'),'\n// altered');
  const r=run(f); assert.equal(r.status,1); assert.match(r.stderr,/integrity/i); assert.equal(r.stdout,'');
});
test('argv cwd stdin stdout stderr and nonzero status survive transport', posix, t => {
  const f=fixture(t,'exit'); const args=['doctor','space value',`quote'"value`,'--','$(literal)'];
  const r=run(f,args); assert.equal(r.status,23); assert.match(r.stderr,/transport stderr/);
  const observed=JSON.parse(r.stdout); assert.equal(observed.cwd,f.root); assert.equal(observed.stdin,'input bytes\n');
  assert.deepEqual(observed.args.slice(-args.length),args);
  assert.deepEqual(observed.args.slice(0,2),['-I','-c']);assert.match(observed.args[2],/sys.argv\[0\]='brigade'/);
  const bootstrap=JSON.parse(fs.readFileSync(path.join(f.root,'state','bootstrap.json')));
  assert.ok(bootstrap.args.includes('--managed-python')); assert.ok(bootstrap.args.includes('--no-index')); assert.equal(bootstrap.args.at(-1),path.join(f.pkg,'wheel','brigade_cli-0.29.0.dev1-py3-none-any.whl'));
});
test('inherited uv/project/Python configuration cannot redirect private runtime', posix, t => {
  const f=fixture(t);
  fs.writeFileSync(path.join(f.root,'uv.toml'),'invalid project config');
  const r=run(f,[],{UV_INDEX_URL:'https://invalid.example',UV_PYTHON:'system',UV_TOOL_DIR:'/unwanted',UV_CONFIG_FILE:'/unwanted',UV_NO_CACHE:'true',UV_PYTHON_DOWNLOADS:'never',UV_OFFLINE:'true',UV_NATIVE_TLS:'true',UV_HTTP_TIMEOUT:'7',BRIGADE_EXAMPLE:'preserved',PYTHONPATH:'/unwanted',VIRTUAL_ENV:'/unwanted'});
  assert.equal(r.status,0);
  const e=JSON.parse(fs.readFileSync(path.join(f.root,'state','bootstrap.json'))).env;
  for(const name of ['UV_INDEX_URL','UV_PYTHON','UV_CONFIG_FILE','UV_NO_CACHE','PYTHONPATH','VIRTUAL_ENV']) assert.equal(e[name],undefined,name);
  assert.equal(e.UV_OFFLINE,'true'); assert.equal(e.UV_PYTHON_DOWNLOADS,'never'); assert.equal(e.UV_NATIVE_TLS,'true'); assert.equal(e.UV_HTTP_TIMEOUT,'7');
  assert.equal(e.BRIGADE_EXAMPLE,'preserved');
  const application=JSON.parse(r.stdout).env;
  assert.equal(application.UV_INDEX_URL,'https://invalid.example');assert.equal(application.UV_TOOL_DIR,'/unwanted');
  assert.equal(application.PYTHONPATH,'/unwanted');assert.equal(application.VIRTUAL_ENV,'/unwanted');
  assert.equal(application.PATH,process.env.PATH);assert.equal(application.BRIGADE_NO_UPDATE_CHECK,process.env.BRIGADE_NO_UPDATE_CHECK);
  assert.equal(e.UV_MANAGED_PYTHON,'true'); assert.equal(e.UV_PYTHON_CPYTHON_BUILD,'20260101');
  for(const name of ['UV_CACHE_DIR','UV_TOOL_DIR','UV_TOOL_BIN_DIR','UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR']) assert.ok(e[name].startsWith(path.join(f.root,'state')),name);
});
test('execution permission errors are actionable', posix, t => {
  const f=fixture(t); fs.chmodSync(path.join(f.companion,'uv'),0o644);
  const r=run(f); assert.equal(r.status,1); assert.match(r.stderr,/permission|EACCES/i);
});
test('SIGTERM reaches application and preserves its normal handled exit code', posix, async t => {
  const f=fixture(t,'wait'); const done=path.join(f.root,'done');
  const child=spawn(process.execPath,[f.launcher],{cwd:f.root,env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state'),TRANSPORT_DONE:done},stdio:['ignore','pipe','pipe']});
  t.after(()=>child.kill('SIGKILL'));
  await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(new Error('transport never became ready')),5000); child.stdout.once('data',()=>{clearTimeout(timeout);resolve();});child.once('exit',()=>{clearTimeout(timeout);reject(new Error('launcher exited before readiness'));});});
  const exited=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
  child.kill('SIGTERM'); const result=await exited;
  assert.equal(fs.readFileSync(done,'utf8'),'terminated'); assert.deepEqual(result,{code:0,signal:null});
});

test('build archive reader accepts official Windows wrappers and rejects unsafe entries', posix, t => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'brigade-archive-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const script=String.raw`
import importlib.util, io, sys, tarfile, zipfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('prepare',sys.argv[1])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
archive=root/'uv-test.zip'
with zipfile.ZipFile(archive,'w') as z:
    z.writestr('uv.exe',b'expected executable')
    z.writestr('uvx.exe',b'ignored wrapper')
    z.writestr('uvw.exe',b'ignored GUI wrapper')
assert p.executable_from_archive(archive,'uv.exe') == b'expected executable'
for bad in ('../uv.exe','/uv.exe','unexpected.exe'):
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('uv.exe',b'expected executable'); z.writestr(bad,b'bad')
    try: p.executable_from_archive(archive,'uv.exe')
    except ValueError: pass
    else: raise AssertionError('unsafe zip entry accepted: '+bad)
archive=root/'uv-test.tar.gz'
with tarfile.open(archive,'w:gz') as z:
    m=tarfile.TarInfo('uv-test/uv'); m.type=tarfile.SYMTYPE; m.linkname='/outside'; z.addfile(m)
try: p.executable_from_archive(archive,'uv')
except ValueError: pass
else: raise AssertionError('symlink accepted')
print('official wrappers accepted; traversal, unexpected paths, and symlinks rejected')
`;
  const r=spawnSync('python3',['-c',script,path.join(__dirname,'prepare.py'),root],{encoding:'utf8'});
  assert.equal(r.status,0,r.stderr);
});

test('packaged Brigade verification child receives original caller UV settings and PATH', { ...posix, skip: (!process.env.BRIGADE_NPM_REGRESSION_STATE || !process.env.BRIGADE_NPM_REGRESSION_ARTIFACTS) ? 'focused real-artifact reproduction needs existing state; fresh acceptance always covers this boundary' : posix.skip }, t => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'brigade-real-env-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const pkg=path.join(root,'launcher'); fs.mkdirSync(pkg);
  fs.copyFileSync(source,path.join(pkg,'launcher.cjs'));
  const packed=path.join(process.env.BRIGADE_NPM_REGRESSION_ARTIFACTS,'packages','launcher');
  fs.copyFileSync(path.join(packed,'runtime.json'),path.join(pkg,'runtime.json'));
  fs.cpSync(path.join(packed,'wheel'),path.join(pkg,'wheel'),{recursive:true});
  const pin=require('./runtime.json').platforms[`${process.platform}-${process.arch}`];
  const companion=path.join(root,'node_modules',...pin.package.split('/'));
  fs.mkdirSync(companion,{recursive:true});
  const old=path.join(process.env.BRIGADE_NPM_REGRESSION_ARTIFACTS,'packages',`${process.platform}-${process.arch}`);
  for(const name of [pin.executable,'manifest.json','package.json']) fs.copyFileSync(path.join(old,name),path.join(companion,name));
  fs.chmodSync(path.join(companion,pin.executable),0o755);
  const callerBin=path.join(root,'caller-bin');fs.mkdirSync(callerBin);
  for(const name of ['sh','dirname','realpath']) {const utility=process.env.PATH.split(path.delimiter).map(d=>path.join(d,name)).find(p=>fs.existsSync(p));fs.symlinkSync(utility,path.join(callerBin,name));}
  const selected={UV_CACHE_DIR:path.join(root,'caller-cache'),UV_TOOL_DIR:path.join(root,'caller-tools'),UV_PYTHON:'caller-python',UV_INDEX_URL:'https://caller.invalid/simple',PATH:callerBin,PYTHONPATH:'/fake-caller-path',VIRTUAL_ENV:'/fake-caller-venv'};
  const code=`console.log(JSON.stringify(Object.fromEntries(${JSON.stringify(Object.keys(selected))}.map(k=>[k,process.env[k]]))))`;
  const r=spawnSync(process.execPath,[path.join(pkg,'launcher.cjs'),'work','verify','run','--target',root,'--argv-json',JSON.stringify([process.execPath,'-e',code])],{encoding:'utf8',env:{...process.env,...selected,BRIGADE_NPM_CACHE:process.env.BRIGADE_NPM_REGRESSION_STATE}});
  assert.equal(r.status,0,r.stderr);
  const dirs=fs.readdirSync(path.join(root,'.brigade/work/verify-runs'));
  const observed=JSON.parse(fs.readFileSync(path.join(root,'.brigade/work/verify-runs',dirs[0],'command-1-stdout.log'),'utf8').trim());
  assert.deepEqual(observed,selected);
});

test('POSIX terminal group SIGINT reaches attached application', posix, async t => {
  const f=fixture(t,'wait'); const done=path.join(f.root,'done');
  const child=spawn(process.execPath,[f.launcher],{detached:true,env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state'),TRANSPORT_DONE:done},stdio:['ignore','pipe','pipe']});
  t.after(()=>{try {process.kill(-child.pid,'SIGKILL');} catch {}});
  const data=await new Promise(resolve=>child.stdout.once('data',resolve));
  const pid=JSON.parse(data.toString()).pid;
  t.after(()=>{try {process.kill(pid,'SIGKILL');} catch {}});
  const pgid=spawnSync('ps',['-o','pgid=','-p',String(pid)],{encoding:'utf8'});
  assert.equal(Number(pgid.stdout.trim()),child.pid,'application must stay in launcher process group');
  const end=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
  process.kill(-child.pid,'SIGINT'); assert.deepEqual(await end,{code:0,signal:null});
  assert.equal(fs.readFileSync(done,'utf8'),'terminated');
});

test('artifact preparation rejects absent executable pins before downloading', posix, t => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'brigade-pins-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const script=String.raw`
import importlib.util, json, subprocess, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('prepare',sys.argv[1])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
temporary=Path(sys.argv[2]); root=temporary/'source'; root.mkdir()
runtime=json.loads((p.SOURCE/'runtime.json').read_text())
runtime['platforms']['linux-x64']['binarySha256']=None
(root/'runtime.json').write_text(json.dumps(runtime)); p.SOURCE=root; p.REPO=root
subprocess.run(['git','init','-q',str(root)],check=True)
subprocess.run(['git','-C',str(root),'add','.'],check=True)
subprocess.run(['git','-C',str(root),'-c','user.name=Example','-c','user.email=example@example.invalid','commit','-qm','fixture'],check=True)
p.download=lambda *args: (_ for _ in ()).throw(AssertionError('download attempted before pin validation'))
sys.argv=['prepare','--out',str(temporary/'artifacts')]
try: p.main()
except ValueError as error: assert 'executable' in str(error) and 'pin' in str(error)
else: raise AssertionError('missing pin accepted')
`;
  const r=spawnSync('python3',['-c',script,path.join(__dirname,'prepare.py'),root],{encoding:'utf8'});
  assert.equal(r.status,0,r.stderr);
});

test('missing executable reports a reinstall action', t => {
  const f=fixture(t);fs.rmSync(path.join(f.companion,'uv'));
  const r=run(f);assert.equal(r.status,1);assert.match(r.stderr,/executable missing.*Reinstall/i);
});

test('non-cancellation child signal becomes numeric status without signalling Node', posix, t => {
  const f=fixture(t,'other-signal');const r=run(f);
  assert.equal(r.signal,null);assert.equal(r.status,128+os.constants.signals.SIGUSR2);
});

test('bootstrap cancellation cannot report success without running the application', posix, async t => {
  const f=fixture(t);
  const binary=path.join(f.companion,'uv');
  const ready=path.join(f.root,'bootstrap-ready');
  const finished=path.join(f.root,'bootstrap-finished');
  // Finish successfully before the old launcher's 250 ms signal forwarding.
  const script=`#!${process.execPath}\nconst fs=require('node:fs');fs.writeFileSync(${JSON.stringify(ready)},'ready');setTimeout(()=>{fs.writeFileSync(${JSON.stringify(finished)},'finished');process.exit(0);},100);`;
  fs.writeFileSync(binary,script,{mode:0o755});
  const hash=crypto.createHash('sha256').update(script).digest('hex');
  const manifest=JSON.parse(fs.readFileSync(path.join(f.companion,'manifest.json')));manifest.sha256=hash;fs.writeFileSync(path.join(f.companion,'manifest.json'),JSON.stringify(manifest));
  const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));runtime.platforms[`${process.platform}-${process.arch}`].binarySha256=hash;fs.writeFileSync(path.join(f.pkg,'runtime.json'),JSON.stringify(runtime));
  const child=spawn(process.execPath,[f.launcher],{detached:true,env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state')},stdio:['ignore','pipe','pipe']});
  t.after(()=>{try{process.kill(-child.pid,'SIGKILL');}catch{}});
  await new Promise((resolve,reject)=>{const poll=setInterval(()=>{if(fs.existsSync(ready)){clearInterval(poll);clearTimeout(timer);resolve();}},10);const timer=setTimeout(()=>{clearInterval(poll);reject(Error('bootstrap did not reach readiness'));},5000);});
  const end=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('cancelled launcher did not exit')),5000);child.once('exit',(code,signal)=>{clearTimeout(timer);resolve({code,signal});});});child.kill('SIGTERM');
  const result=await end;t.diagnostic(`bootstrap cancellation exit: ${JSON.stringify(result)}`);assert.equal(fs.readFileSync(finished,'utf8'),'finished');assert.ok(result.signal==='SIGTERM'||result.code===143,JSON.stringify(result));
});

test('slow SIGINT cleanup receives one terminal group signal', posix, async t => {
  const f=fixture(t,'slow-wait');const done=path.join(f.root,'done');
  const child=spawn(process.execPath,[f.launcher],{detached:true,env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state'),TRANSPORT_DONE:done},stdio:['ignore','pipe','pipe']});
  t.after(()=>{try {process.kill(-child.pid,'SIGKILL');}catch {}});
  await new Promise(resolve=>child.stdout.once('data',resolve));
  const end=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
  process.kill(-child.pid,'SIGINT');assert.deepEqual(await end,{code:0,signal:null});assert.equal(fs.readFileSync(done,'utf8'),'terminated');
});

async function waitForFile(file) {
  const deadline=Date.now()+5000;
  while(!fs.existsSync(file)) {
    if(Date.now()>deadline) throw new Error(`transport never wrote ${path.basename(file)}`);
    await new Promise(resolve=>setTimeout(resolve,10));
  }
}

for(const firstSignal of ['SIGTERM','SIGINT']) {
  test(`repeated application cancellation preserves cleanup and child exit after ${firstSignal}`, { ...posix, timeout:8000 }, async t => {
    const f=fixture(t,'cleanup-wait');const done=path.join(f.root,'done');
    const ready=path.join(f.root,'ready');const started=path.join(f.root,'started');
    const child=spawn(process.execPath,[f.launcher],{detached:true,env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state'),TRANSPORT_READY:ready,TRANSPORT_STARTED:started,TRANSPORT_DONE:done},stdio:['ignore','pipe','pipe']});
    t.after(()=>{if(child.exitCode===null && child.signalCode===null) {try{process.kill(-child.pid,'SIGKILL');}catch{}}});
    await waitForFile(ready);
    const end=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
    assert.equal(child.kill(firstSignal),true);
    // Same-signal repeats interrupt cleanup. Mixed signals arrive before the
    // first forwarding delay, so only the original SIGINT should reach it.
    if(firstSignal==='SIGTERM') await waitForFile(started);
    else await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(child.kill('SIGTERM'),true);
    const result=await end;
    const marker=fs.existsSync(done)?JSON.parse(fs.readFileSync(done,'utf8')):null;
    t.diagnostic(`application cancellation: exit=${JSON.stringify(result)}, completion=${JSON.stringify(marker)}`);
    assert.deepEqual(result,{code:23,signal:null});
    assert.deepEqual(marker,firstSignal==='SIGTERM'?['SIGTERM','SIGTERM']:['SIGINT']);
  });
}

test('a second bootstrap cancellation signal forces termination before cleanup finishes', { ...posix, timeout:8000 }, async t => {
  const f=fixture(t,'bootstrap-wait');const done=path.join(f.root,'done');
  const ready=path.join(f.root,'ready');const started=path.join(f.root,'started');
  const child=spawn(process.execPath,[f.launcher],{detached:true,env:{...process.env,BRIGADE_NPM_CACHE:path.join(f.root,'state'),TRANSPORT_READY:ready,TRANSPORT_STARTED:started,TRANSPORT_DONE:done},stdio:['ignore','pipe','pipe']});
  t.after(()=>{if(child.exitCode===null && child.signalCode===null) {try{process.kill(-child.pid,'SIGKILL');}catch{}}});
  await waitForFile(ready);
  const end=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
  assert.equal(child.kill('SIGTERM'),true);
  await waitForFile(started);
  assert.equal(child.kill('SIGTERM'),true);
  const result=await end;
  t.diagnostic(`bootstrap cancellation: exit=${JSON.stringify(result)}, completion=${fs.existsSync(done)}`);
  assert.deepEqual(result,{code:137,signal:null});assert.ok(!fs.existsSync(done));
  assert.ok(!fs.existsSync(path.join(f.root,'state','bootstrap.json')));
});

// Owner regressions use the launched executable boundary, never production test hooks.
test('corrupt bundled wheel is rejected before uv executes', posix, t => {
 const f=fixture(t);fs.appendFileSync(path.join(f.pkg,'wheel','brigade_cli-0.29.0.dev1-py3-none-any.whl'),'corrupt');
 const r=run(f);assert.equal(r.status,1);assert.match(r.stderr,/wheel.*(integrity|SHA|size)/i);
 assert.ok(!fs.existsSync(path.join(f.root,'state','bootstrap.json')));
});
test('warm marker skips uv and mismatched marker or missing interpreter recovers', posix, t => {
 const f=fixture(t);assert.equal(run(f).status,0);
 const state=path.join(f.root,'state');const count=()=>fs.readFileSync(path.join(state,'install-count'),'utf8').length;
 assert.equal(count(),1);assert.equal(run(f).status,0);assert.equal(count(),1);
 const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
 const e=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:state},runtime);
 const marker=path.join(path.dirname(e.UV_TOOL_DIR),'initialized.json');
 const m=JSON.parse(fs.readFileSync(marker));m.wheelSha256='0'.repeat(64);fs.writeFileSync(marker,JSON.stringify(m));
 assert.equal(run(f).status,0);assert.equal(count(),2);
 fs.rmSync(require(f.launcher).toolPython(e));assert.equal(run(f).status,0);assert.equal(count(),3);
});
test('private identity file missing corrupt or misdirected is repaired or rejected', posix, t => {
 const f=fixture(t);assert.equal(run(f).status,0);
 const state=path.join(f.root,'state');const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
 const e=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:state},runtime);
 const marker=path.join(path.dirname(e.UV_TOOL_DIR),'initialized.json');
 const identity=JSON.parse(fs.readFileSync(marker)).identity;
 for(const mutation of ['missing','corrupt','symlink']) {
  fs.rmSync(identity.path,{force:true});
  if(mutation==='corrupt') fs.writeFileSync(identity.path,'bad');
  if(mutation==='symlink') {const outside=path.join(f.root,'outside');fs.writeFileSync(outside,'untouched');fs.symlinkSync(outside,identity.path);}
  const r=run(f);assert.ok(r.status===0||r.status===1);
  if(r.status===0) {assert.ok(fs.lstatSync(identity.path).isFile());assert.equal(crypto.createHash('sha256').update(fs.readFileSync(identity.path)).digest('hex'),identity.sha256);}
  else assert.ok(!fs.existsSync(marker));
  if(mutation==='symlink') assert.equal(fs.readFileSync(path.join(f.root,'outside'),'utf8'),'untouched');
 }
});
test('invalid private interpreter purelib cannot initialize', posix, t => {
 const f=fixture(t,'identity-invalid');const r=run(f);assert.equal(r.status,1);assert.match(r.stderr,/private|purelib|identity/i);
 const markers=[];function walk(d) {if(!fs.existsSync(d))return;for(const name of fs.readdirSync(d)){const p=path.join(d,name);if(fs.lstatSync(p).isDirectory())walk(p);else if(name==='initialized.json')markers.push(p);}}
 walk(path.join(f.root,'state'));assert.deepEqual(markers,[]);
});

test('warm marker cannot redirect identity to an unused directory inside the venv', posix, t => {
 const f=fixture(t);assert.equal(run(f).status,0);
 const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
 const e=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:path.join(f.root,'state')},runtime);
 const marker=path.join(path.dirname(e.UV_TOOL_DIR),'initialized.json');const m=JSON.parse(fs.readFileSync(marker));
 const actual=m.identity.path;const wrong=path.join(e.UV_TOOL_DIR,'brigade-cli','unused','_brigade_npm.pth');
 fs.mkdirSync(path.dirname(wrong));fs.copyFileSync(actual,wrong);fs.rmSync(actual);m.identity.path=wrong;fs.writeFileSync(marker,JSON.stringify(m));
 const r=run(f);assert.ok(r.status===0||r.status===1);
 if(r.status===0) {assert.equal(JSON.parse(fs.readFileSync(marker)).identity.path,actual);assert.ok(fs.existsSync(actual));}
 else assert.ok(!fs.existsSync(marker));
});

test('identity write failure leaves no marker and correct later startup recovers', posix, t => {
 const f=fixture(t);assert.equal(run(f).status,0);
 const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
 const e=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:path.join(f.root,'state')},runtime);
 const marker=path.join(path.dirname(e.UV_TOOL_DIR),'initialized.json');const m=JSON.parse(fs.readFileSync(marker));
 fs.rmSync(m.identity.path);fs.mkdirSync(m.identity.path);
 const failed=run(f);assert.equal(failed.status,1);assert.ok(!fs.existsSync(marker));
 fs.rmSync(m.identity.path,{recursive:true});assert.equal(run(f).status,0);assert.ok(fs.existsSync(marker));
});

test('trusted cache aliases reuse cold and warm state while owned symlinks are rejected', posix, t => {
 const f=fixture(t);const canonical=path.join(f.root,'cache-real');fs.mkdirSync(canonical);
 const alias=path.join(f.root,'cache-alias');fs.symlinkSync(canonical,alias,'dir');
 const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
 const state=path.join(canonical,'state'),aliased=path.join(alias,'state');
 const e=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:state},runtime);
 const marker=path.join(path.dirname(e.UV_TOOL_DIR),'initialized.json');
 for(const cache of [aliased,state,aliased,state]) {
  const r=run(f,[],{BRIGADE_NPM_CACHE:cache});assert.equal(r.status,0,r.stderr);
  assert.equal(fs.readFileSync(path.join(state,'install-count'),'utf8'),'1');
 }
 const stable=fs.readFileSync(marker);
 assert.equal(run(f,[],{BRIGADE_NPM_CACHE:aliased}).status,0);assert.deepEqual(fs.readFileSync(marker),stable);
 for(const owned of [path.join(e.UV_TOOL_DIR,'brigade-cli'),e.UV_TOOL_DIR]) {
  const saved=owned+'-saved';fs.renameSync(owned,saved);
  const outside=fs.mkdtempSync(path.join(f.root,'untrusted-'));fs.writeFileSync(path.join(outside,'sentinel'),'untouched');
  fs.symlinkSync(outside,owned,'dir');
  const r=run(f,[],{BRIGADE_NPM_CACHE:aliased});assert.equal(r.status,1,r.stderr);
  assert.ok(!fs.existsSync(marker));assert.deepEqual(fs.readdirSync(outside),['sentinel']);
  assert.equal(fs.readFileSync(path.join(outside,'sentinel'),'utf8'),'untouched');
  fs.unlinkSync(owned);fs.renameSync(saved,owned);
 }
});

for(const warm of [true,false]) {
 test(`${warm?'warm':'cold'} private identity lookup cancellation preserves first signal and initialization state`, {...posix,timeout:8000}, async t => {
  const f=fixture(t);const state=path.join(f.root,'state');
  const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
  const e=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:state},runtime);
  const marker=path.join(path.dirname(e.UV_TOOL_DIR),'initialized.json');
  if(warm) assert.equal(run(f).status,0);
  const before=warm?fs.readFileSync(marker):null;
  const ready=path.join(f.root,'identity-ready');
  const child=spawn(process.execPath,[f.launcher],{detached:true,cwd:f.root,env:{...process.env,BRIGADE_NPM_CACHE:state,TRANSPORT_IDENTITY_READY:ready},stdio:['ignore','pipe','pipe']});
  t.after(()=>{if(child.exitCode===null && child.signalCode===null){try{process.kill(-child.pid,'SIGKILL');}catch{}}});
  const end=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
  await waitForFile(ready);
  const firstSignal=warm?'SIGTERM':'SIGINT';assert.equal(child.kill(firstSignal),true);
  const result=await end;
  const installs=fs.readFileSync(path.join(state,'install-count'),'utf8').length;
  const after=fs.existsSync(marker)?fs.readFileSync(marker):null;
  t.diagnostic(JSON.stringify({warm,firstSignal,result,installs,markerPresent:after!==null,markerStable:before!==null&&after!==null&&before.equals(after)}));
  assert.ok(result.signal===firstSignal||result.code===128+os.constants.signals[firstSignal],JSON.stringify(result));
  assert.equal(installs,1,'cancellation must not spawn another uv install');
  assert.deepEqual(after,before,'warm marker bytes must survive cancellation; cold must leave none');
 });
}


test('writable shared cache root is refused without changing its permissions', posix, t => {
 const f=fixture(t);const state=path.join(f.root,'state');fs.mkdirSync(state,{mode:0o777});fs.chmodSync(state,0o777);
 const r=run(f);assert.equal(r.status,1,r.stderr);assert.match(r.stderr,/cache.*(owner|permission|writ)/i);
 assert.equal(fs.statSync(state).mode&0o777,0o777,'unsafe existing root must not be chmodded');
 assert.deepEqual(fs.readdirSync(state),[],'refuse before uv or owned cache writes');
});

for(const key of ['UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','UV_PYTHON_CACHE_DIR']) {
 test(`managed Python ${key} redirects are refused before cold and warm launch and after provisioning`, posix, t => {
  for(const stage of ['cold','warm','provisioned']) {
   const f=fixture(t);const state=path.join(f.root,'state');
   const runtime=JSON.parse(fs.readFileSync(path.join(f.pkg,'runtime.json')));
   const selected=require(f.launcher).runtimeEnvironment({BRIGADE_NPM_CACHE:state},runtime)[key];
   if(stage==='warm') assert.equal(run(f).status,0);
   const outside=path.join(f.root,'outside');fs.mkdirSync(outside);fs.writeFileSync(path.join(outside,'sentinel'),'untouched');
   const extra=stage==='provisioned'?{TRANSPORT_REDIRECT_PYTHON:key,TRANSPORT_OUTSIDE:outside}:{};
   if(stage!=='provisioned') {fs.mkdirSync(path.dirname(selected),{recursive:true});fs.symlinkSync(outside,selected,'dir');}
   const count=path.join(state,'install-count');const before=fs.existsSync(count)?fs.readFileSync(count,'utf8'):'';
   const r=run(f,[],extra);assert.equal(r.status,1,stage+': '+r.stderr);assert.equal(r.stdout,'',stage);
   assert.match(r.stderr,/non-symlink directory/,stage);
   assert.equal(fs.existsSync(count)?fs.readFileSync(count,'utf8'):'',before+(stage==='provisioned'?'1':''),stage+': no unexpected uv install');
   assert.deepEqual(fs.readdirSync(outside),['sentinel']);assert.equal(fs.readFileSync(path.join(outside,'sentinel'),'utf8'),'untouched');
  }
 });
}

for (const variant of ['valid','filename-mismatch','missing-version']) test(`stable wheel exact runtime filename (${variant})`,posix,t=>{
  const f=fixture(t);const file=path.join(f.pkg,'runtime.json');const r=JSON.parse(fs.readFileSync(file));
  r.payloadVersion=r.payload.version='0.29.0';
  const old=path.join(f.pkg,'wheel',r.payload.wheel);r.payload.wheel='brigade_cli-0.29.0-py3-none-any.whl';
  fs.renameSync(old,path.join(f.pkg,'wheel',r.payload.wheel));
  if(variant==='filename-mismatch') r.payload.wheel='brigade_cli-0.29.1-py3-none-any.whl';
  if(variant==='missing-version') delete r.payloadVersion;
  fs.writeFileSync(file,JSON.stringify(r));const result=run(f,['--version']);
  assert.equal(result.status,variant==='valid'?0:1,result.stderr);
  if(variant!=='valid') assert.match(result.stderr,/manifest invalid/);
});
