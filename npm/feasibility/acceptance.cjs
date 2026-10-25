'use strict';
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');
const zlib=require('node:zlib');
const {spawn,spawnSync}=require('node:child_process');
const {performance}=require('node:perf_hooks');

const args=process.argv.slice(2);
function option(name) { const i=args.indexOf(name); if(i<0 || !args[i+1]) throw new Error(`Required: ${name}`); return path.resolve(args[i+1]); }
const registryIndex=args.indexOf('--registry-spec');
const registrySpec=registryIndex<0?null:args[registryIndex+1];
const registryMode=registrySpec!==null;
const containedUncatchable=args.includes('--contained-linux-uncatchable');
const pidNamespace=process.platform==='linux'?fs.readlinkSync('/proc/self/ns/pid'):null;
const externalHostNamespace=process.env.BRIGADE_NPM_EXTERNAL_HOST_PID_NAMESPACE || null;
if(containedUncatchable && (process.platform!=='linux' || !/^pid:\[\d+\]$/.test(externalHostNamespace || '') || pidNamespace===externalHostNamespace)) {
  throw new Error('Contained uncatchable acceptance requires Linux with an externally observed, separate host PID namespace and bounded external teardown.');
}
const artifactsDir=option('--artifacts');
const out=option('--out');
fs.mkdirSync(out,{recursive:true});
const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'brigade-npm-acceptance-')));
const receipt={schema:1,startedAt:new Date().toISOString(),platform:`${process.platform}-${process.arch}`,nodeVersion:process.version,npmVersion:null,osRelease:os.release(),machine:process.platform==='win32'?process.env.PROCESSOR_ARCHITECTURE:spawnSync('uname',['-m'],{encoding:'utf8'}).stdout.trim(),rosetta:process.platform==='darwin'?spawnSync('sysctl',['-n','sysctl.proc_translated'],{encoding:'utf8'}).stdout.trim() || 'unavailable':null,status:'running',root,checks:[],artifacts:[],limitations:['Hosted coverage requires separate native jobs.','Managed Python network transfer bytes are not metered.']};
// Namespace metadata records the external runner's operation, not a security boundary.
receipt.distributionMode=registryMode?'registry':'packed';
receipt.acceptanceMode=containedUncatchable?'contained-linux-uncatchable':'native-default';
receipt.pidNamespace=pidNamespace;receipt.externalHostPidNamespace=externalHostNamespace;
const reportPath=path.join(out,'acceptance.json');
function save() { fs.writeFileSync(reportPath,JSON.stringify(receipt,null,2)+'\n'); }
const secrets=Object.entries(process.env).filter(([key,value])=>value && /TOKEN|PASSWORD|SECRET|KEY|AUTH|PROXY|MIRROR|INDEX_URL|CERT|APPROVAL_CORRELATION/i.test(key)).map(([,value])=>value).sort((a,b)=>b.length-a.length);
function redact(value) {
  let s=String(value);
  for(const secret of secrets) if(secret.length>=4) s=s.split(secret).join('[redacted]');
  return s.replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g,'$1[redacted]@').slice(0,64000);
}
function record(name,result,extra={}) { const check={name,...result,...extra}; receipt.checks.push(check); save(); return check; }
function usage(dir) {
  const result={logicalBytes:0,uniqueFileBytes:0,allocatedBytes:process.platform==='win32'?null:0};
  const seen=new Set();
  function walk(directory) {
    if(!fs.existsSync(directory)) return;
    for(const e of fs.readdirSync(directory,{withFileTypes:true})) {
      const p=path.join(directory,e.name);
      if(e.isDirectory()) walk(p);
      else if(e.isFile()) {
        const stat=fs.statSync(p);
        result.logicalBytes+=stat.size;
        const identity=stat.ino?`${stat.dev}:${stat.ino}`:p;
        if(!seen.has(identity)) {
          seen.add(identity);result.uniqueFileBytes+=stat.size;
          if(result.allocatedBytes!==null) result.allocatedBytes+=stat.blocks*512;
        }
      }
    }
  }
  walk(dir);return result;
}
function size(dir) { return usage(dir).uniqueFileBytes; }
function resolveNpm() {
  function installed(cli) {
    try {
      const real=fs.realpathSync(cli);
      if(fs.statSync(real).isFile() && fs.statSync(path.join(path.dirname(real),'npx-cli.js')).isFile()) return real;
    } catch {}
    return null;
  }
  if(process.env.npm_execpath) {
    const explicit=installed(process.env.npm_execpath);
    if(!explicit) throw new Error('npm_execpath must select an npm CLI file with a sibling npx-cli.js.');
    return explicit;
  }
  for(const dir of (process.env.PATH || '').split(path.delimiter)) {
    const command=path.join(dir,process.platform==='win32'?'npm.cmd':'npm');
    try {
      if(!fs.statSync(command).isFile()) continue;
      if(process.platform!=='win32') fs.accessSync(command,fs.constants.X_OK);
      const real=fs.realpathSync(command);
      // POSIX symlinks identify the CLI directly; npm.cmd uses the adjacent tree.
      const candidates=[...(real.endsWith('.js')?[real]:[]),path.join(path.dirname(real),'node_modules','npm','bin','npm-cli.js'),path.join(dir,'node_modules','npm','bin','npm-cli.js')];
      for(const cli of candidates) {const found=installed(cli);if(found) return found;}
    } catch {}
  }
  for(const cli of [path.join(path.dirname(process.execPath),'node_modules','npm','bin','npm-cli.js'),path.join(path.dirname(process.execPath),'..','lib','node_modules','npm','bin','npm-cli.js')]) {
    const found=installed(cli);if(found) return found;
  }
  throw new Error('Cannot locate npm CLI JavaScript. Set npm_execpath to the installed npm-cli.js.');
}
let npmCli,npxCli,npxEnv,npxPackages,home,prefix,bin,env,gitTemplate,gitDir;
function setup() {
npmCli=resolveNpm();
npxCli=path.join(path.dirname(npmCli),'npx-cli.js');assert.ok(fs.existsSync(npxCli));receipt.npxCli=npxCli;
const versionProbe=spawnSync(process.execPath,[npmCli,'--version'],{encoding:'utf8',timeout:10000,maxBuffer:64000});
const npmVersion=(versionProbe.stdout || '').trim();
if(versionProbe.status!==0 || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(npmVersion)) throw new Error('Selected npm CLI version probe failed.');
receipt.npmVersion=npmVersion;
const expectedIndex=args.indexOf('--expected-platform');
receipt.expectedPlatform=expectedIndex>=0?args[expectedIndex+1]:receipt.platform;
assert.equal(receipt.platform,receipt.expectedPlatform,'native matrix platform must match');
home=path.join(root,'home');
prefix=path.join(root,'prefix');
bin=path.join(root,'path');
for(const p of [home,prefix,bin]) fs.mkdirSync(p,{recursive:true});
gitDir='';
if(process.platform==='win32') {
  fs.copyFileSync(process.execPath,path.join(bin,'node.exe'));
  const found=spawnSync('where.exe',['git.exe'],{encoding:'utf8'});
  assert.equal(found.status,0,found.stderr);
  gitDir=path.dirname(found.stdout.trim().split(/\r?\n/)[0]);
}
else {
  fs.symlinkSync(process.execPath,path.join(bin,'node'));
  for(const name of ['sh','dirname','realpath','git']) {
    const utility=(process.env.PATH || '').split(path.delimiter).map(dir=>path.join(dir,name)).find(p=>fs.existsSync(p));
    if(!utility) throw new Error(`Required OS utility missing from acceptance host: ${name}`);
    fs.symlinkSync(fs.realpathSync(utility),path.join(bin,name));
  }
}
// Node and required OS utilities are on the acceptance PATH. No Python/uv.
const inherited={...process.env};
for(const key of Object.keys(inherited)) if(/^npm_/i.test(key) || /^(NODE_AUTH_TOKEN|NPM_TOKEN)$/i.test(key)) delete inherited[key];
env={...inherited,HOME:home,USERPROFILE:home,APPDATA:path.join(home,'appdata'),LOCALAPPDATA:path.join(home,'local'),XDG_CONFIG_HOME:path.join(home,'config'),XDG_CACHE_HOME:path.join(home,'cache'),PATH:bin+(process.platform==='win32'?path.delimiter+path.join(process.env.SystemRoot || 'C:\\Windows','System32')+path.delimiter+gitDir:''),BRIGADE_NPM_CACHE:path.join(root,'warm-state'),npm_config_cache:path.join(root,'npm-cache'),npm_config_offline:registryMode?'false':'true',npm_config_registry:'https://registry.npmjs.org/',npm_config_audit:'false',npm_config_fund:'false',npm_config_userconfig:path.join(home,'empty-npmrc'),npm_config_globalconfig:path.join(home,'empty-global-npmrc')};
for(const key of Object.keys(env)) if(key.startsWith('BRIGADE_') && key!=='BRIGADE_NPM_CACHE') delete env[key];
for(const key of Object.keys(env)) if(key.toUpperCase().startsWith('GIT_')) delete env[key];
const gitConfig=path.join(home,'empty-gitconfig');fs.writeFileSync(gitConfig,'');
gitTemplate=path.join(home,'empty-git-template');fs.mkdirSync(gitTemplate);
Object.assign(env,{GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_SYSTEM:gitConfig,GIT_CONFIG_GLOBAL:gitConfig,GIT_TEMPLATE_DIR:gitTemplate});
fs.writeFileSync(env.npm_config_userconfig,''); fs.writeFileSync(env.npm_config_globalconfig,'');
}
// Build stdout/stderr are kept bounded and sanitized, never an environment object.
function terminateTree(child) {
  if(process.platform==='win32') {
    const taskkill=path.join(process.env.SystemRoot || 'C:\\Windows','System32','taskkill.exe');
    spawn(taskkill,['/PID',String(child.pid),'/T','/F'],{stdio:'ignore'}).once('error',()=>child.kill('SIGKILL'));
  } else child.kill('SIGTERM');
}
function execute(executable,argv,options={}) {
  return new Promise((resolve,reject)=>{
    const start=performance.now();
    let stdout='',stderr='',done=false;
    const child=spawn(executable,argv,{env:options.env || env,cwd:options.cwd || root,stdio:['pipe','pipe','pipe'],shell:false,detached:!!options.processGroup});
    const timer=setTimeout(()=>{terminateTree(child);reject(new Error(`Timeout: ${options.label || executable}`));},options.timeout || 180000);
    child.stdout.on('data',b=>{stdout=(stdout+b.toString()).slice(0,128000); if(options.onOutput) options.onOutput(child,stdout,stderr);});
    child.stderr.on('data',b=>{stderr=(stderr+b.toString()).slice(0,128000); if(options.onOutput) options.onOutput(child,stdout,stderr);});
    child.once('error',error=>{clearTimeout(timer);reject(error);});
    child.once('close',(code,signal)=>{
      done=true;clearTimeout(timer);
      resolve({exit:code,signal,ms:Math.round((performance.now()-start)*100)/100,stdout:redact(stdout),stderr:redact(stderr)});
    });
    child.stdin.end(options.input || '');
    if(options.onSpawn) options.onSpawn(child,()=>done);
  });
}
let launcher,runtime,contract,uv;
async function call(name,argv,extra={}) {
  const result=await execute(process.execPath,[launcher,...argv],extra);
  record(name,result,{args:argv.map(redact)}); return result;
}
function versionOK(result) {assert.equal(result.exit,0,result.stderr);assert.equal(result.stdout.trim(),'brigade '+runtime.payloadVersion);}
function hash(bytes) {return crypto.createHash('sha256').update(bytes).digest('hex');}
function tarFiles(bytes) {
  const tar=zlib.gunzipSync(bytes),files=new Map();
  for(let offset=0;offset+512<=tar.length;) {
    const header=tar.subarray(offset,offset+512),name=header.subarray(0,100).toString().replace(/\0.*$/s,'');
    if(!name) break;
    const length=parseInt(header.subarray(124,136).toString().replace(/\0.*$/s,'').trim(),8)||0;
    assert.ok(!files.has(name));files.set(name,tar.subarray(offset+512,offset+512+length));offset+=512+Math.ceil(length/512)*512;
  }
  return files;
}
async function npxCall(name,argv) {
  const result=await execute(process.execPath,registryMode?[npxCli,'--yes',registrySpec,...argv]:[npxCli,'--offline','--yes',...npxPackages,'--','brigade',...argv],{env:npxEnv});
  record(name,result,{args:argv});return result;
}
async function proveInstalledWheel(privateEnv) {
  const wheel=path.join(path.dirname(launcher),'wheel',runtime.payload.wheel);
  const code=`import base64,csv,hashlib,importlib.metadata as m,io,json,zipfile\nfrom pathlib import Path\nwith zipfile.ZipFile(${JSON.stringify(wheel)}) as z:\n d=m.distribution('brigade-cli'); rows={r[0]:r for r in csv.reader(io.StringIO(z.read('brigade_cli-${runtime.payload.version}.dist-info/RECORD').decode()))}; result={}\n for name in ['brigade/__init__.py','brigade/work_cmd/verify_runtime.py']:\n  data=Path(d.locate_file(name)).read_bytes(); assert data==z.read(name); digest=base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(); assert rows[name][1]=='sha256='+digest; result[name]=hashlib.sha256(data).hexdigest()\n print(json.dumps(result))`;
  const r=await execute(contract.toolPython(privateEnv),['-I','-c',code]);record('installed-wheel-RECORD',r);assert.equal(r.exit,0,r.stderr);
}
async function proveVersionRefresh(privatePython,identityEnv,scratch) {
  const sentinel=path.join(scratch,'forbidden.log');
  // Mock only the endpoint and process launch. Exercise real cache writes and
  // request construction, and fail even if production swallows a socket error.
  const code=`import io,json,os,platform,socket
from pathlib import Path
from contextlib import closing,redirect_stderr
from brigade import __version__,distribution,update_notify as n,update_cmd as u
assert distribution.is_npm()
log=Path(${JSON.stringify(sentinel)})
def denied(*a,**kw):
 log.write_text('forbidden'); raise AssertionError('unexpected update side effect')
socket.socket=denied; socket.create_connection=denied; u.default_paths=denied
requests=[]; spawns=[]
def fetch(request,timeout):
 requests.append((request,timeout)); return closing(io.BytesIO(b'{"latest":"98.0.0"}'))
n.urllib.request.urlopen=fetch
n.subprocess.Popen=lambda *a,**kw: spawns.append((a,kw))
class TTY(io.StringIO):
 def isatty(self): return True
base=dict(os.environ)
for key in ('BRIGADE_NO_UPDATE_CHECK','CI','DO_NOT_TRACK','BRIGADE_TELEMETRY','BRIGADE_INSTALL_CHANNEL'):
 base.pop(key,None)
for extra in ({},{'DO_NOT_TRACK':'1'},{'BRIGADE_TELEMETRY':'0'}):
 os.environ.clear(); os.environ.update(base); os.environ.update(extra)
 p=n.cache_path(os.environ); p.parent.mkdir(parents=True,exist_ok=True)
 p.write_text(json.dumps({'latest':'99.0.0','checked_at':0})); before=p.read_bytes()
 requests.clear(); spawns.clear(); err=TTY()
 n.maybe_notify(['work','brief'],0,now=1000000,stderr=err)
 assert err.getvalue()=='' and n.available_update() is None
 assert len(spawns)==1 and spawns[0][1]['env']['BRIGADE_INSTALL_CHANNEL']=='npm'
 assert spawns[0][0][0][-2:]==['update','--notify-refresh']
 assert requests==[]
 assert n.run_refresh(now=1000000)==0
 assert len(requests)==1
 request,timeout=requests[0]
 assert request.full_url==n.CHECK_URL and request.get_method()=='GET' and request.data is None and timeout==5
 if extra:
  assert request.get_header('User-agent')=='Python-urllib' and request.get_header('X-brigade-channel') is None
 else:
  assert request.get_header('User-agent')==f'brigade-cli/{__version__} ({platform.system()})'
  assert request.get_header('X-brigade-channel')=='npm'
 state=json.loads(p.read_text()); assert state['latest']=='98.0.0' and state['checked_at']==1000000 and p.read_bytes()!=before
 n.maybe_notify(['work','brief'],0,now=1000001,stderr=err)
 assert len(spawns)==1 and len(requests)==1 and err.getvalue()==''
for gate in ('BRIGADE_NO_UPDATE_CHECK','CI'):
 os.environ.clear(); os.environ.update(base); os.environ[gate]='1'
 before=p.read_bytes(); requests.clear(); spawns.clear()
 n.maybe_notify(['work','brief'],0,stderr=TTY()); assert n.run_refresh()==0
 assert requests==[] and spawns==[] and p.read_bytes()==before
n.cache_path=denied
assert n.available_update() is None
with redirect_stderr(io.StringIO()):
 assert u.run_update()==1
assert not log.exists()
print('npm refresh: one bounded mocked GET per refresh, cache updated, notice suppressed, channel and opt-outs verified')`;
  for(const flags of [['-I'],[]]) {
    const guarded=await execute(privatePython,[...flags,'-c',code],{env:identityEnv});
    record('private-respawn-version-refresh-'+(flags.length?'isolated':'normal'),guarded);
    assert.equal(guarded.exit,0,guarded.stderr);assert.ok(!fs.existsSync(sentinel));
  }
}
async function proveIdentity(privateEnv,independent) {
  const privatePython=contract.toolPython(privateEnv);
  const scratch=path.join(root,'identity-home');fs.mkdirSync(scratch,{recursive:true});
  const identityEnv={...env,HOME:scratch,USERPROFILE:scratch,XDG_CACHE_HOME:path.join(scratch,'cache')};
  delete identityEnv.BRIGADE_NO_UPDATE_CHECK;delete identityEnv.BRIGADE_DISTRIBUTION;delete identityEnv.CI;
  const wheelPath=path.join(path.dirname(launcher),'wheel',runtime.payload.wheel);
  const seed=`import json,os,sys,time; sys.path.insert(0,${JSON.stringify(wheelPath)}); from brigade.update_notify import cache_path; p=cache_path(os.environ); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps({'latest':'99.0.0','checked_at':time.time()})); print(str(p))`;
  const seeded=await execute(independent,['-I','-c',seed],{env:identityEnv});assert.equal(seeded.exit,0,seeded.stderr);
  const cache=seeded.stdout.trim(),before=fs.readFileSync(cache);
  for(const isolated of [true,false]) {
    for(const argv of [['work','brief','--target',root],['update']]) {
      const r=await execute(privatePython,[...(isolated?['-I']:[]),'-m','brigade',...argv],{env:identityEnv});
      record('private-respawn-'+(isolated?'isolated':'normal')+'-'+argv.join('-'),r);
      assert.equal(r.exit,argv.length===1?1:0,r.stderr);assert.ok(!/update_available|new brigade release/i.test(r.stdout+r.stderr));
      assert.deepEqual(fs.readFileSync(cache),before);
    }
  }
  await proveVersionRefresh(privatePython,identityEnv,scratch);
  const marker=JSON.parse(fs.readFileSync(path.join(path.dirname(privateEnv.UV_TOOL_DIR),'initialized.json')));
  const caller=await execute(independent,['-I','-c',`import sys; sys.path.insert(0,${JSON.stringify(path.dirname(marker.identity.path))}); from brigade import distribution as d; assert not d.is_npm(); print('independent interpreter unmarked')`],{env:identityEnv});
  assert.equal(caller.exit,0,caller.stderr);record('independent-interpreter-unmarked',caller);
}
async function tamperIdentity(privateEnv) {
  const marker=path.join(path.dirname(privateEnv.UV_TOOL_DIR),'initialized.json');
  const m=JSON.parse(fs.readFileSync(marker));
  for(const mutation of ['missing','corrupt','wrong-path']) {
    if(mutation==='missing') fs.rmSync(m.identity.path);
    if(mutation==='corrupt') fs.writeFileSync(m.identity.path,'# corrupt identity\n');
    if(mutation==='wrong-path') {const changed=JSON.parse(fs.readFileSync(marker));const wrong=path.join(privateEnv.UV_TOOL_DIR,'brigade-cli','unused','_brigade_npm.pth');fs.mkdirSync(path.dirname(wrong),{recursive:true});fs.copyFileSync(changed.identity.path,wrong);fs.rmSync(changed.identity.path);changed.identity.path=wrong;fs.writeFileSync(marker,JSON.stringify(changed));}
    const r=await call('identity-tamper-'+mutation,['--version']);versionOK(r);
    const repaired=JSON.parse(fs.readFileSync(marker));assert.equal(hash(fs.readFileSync(repaired.identity.path)),repaired.identity.sha256);assert.equal(repaired.identity.path,m.identity.path);
  }
}
function identityOf(pid) {
  if(process.platform==='linux') {try {const stat=fs.readFileSync(`/proc/${pid}/stat`,'utf8');return {pid,start:stat.slice(stat.lastIndexOf(')')+2).split(' ')[19]};} catch{return null;}}
  return {pid};
}
function identityAlive(identity) {
  if(process.platform==='linux') {try {const stat=fs.readFileSync(`/proc/${identity.pid}/stat`,'utf8'),parts=stat.slice(stat.lastIndexOf(')')+2).split(' ');return parts[0]!=='Z'&&parts[19]===identity.start;} catch{return false;}}
  try {process.kill(identity.pid,0);return true;}catch{return false;}
}
async function proveCanceledReceipt(target,run) {
  const privateEnv=contract.runtimeEnvironment(env,runtime);
  const code=`import json\nfrom pathlib import Path\nfrom brigade import localio\np=Path(${JSON.stringify(path.join(target,'.brigade/work/verify-runs'))})/${JSON.stringify('PLACEHOLDER')}\nr=json.loads((p/'receipt.json').read_text()); assert r['status']=='canceled',r; assert r['digests']['receipt_sha256']==localio.canonical_json_digest(r,exclude_keys={'digests'})\nfor log,digest in r['digests'].get('logs',{}).items(): assert __import__('hashlib').sha256((p/log).read_bytes()).hexdigest()==digest\nrecords=[json.loads(line) for line in (Path(${JSON.stringify(target)})/'memory/outcome/records.jsonl').read_text().splitlines()]; matches=[row for row in records if ${JSON.stringify(run)} in row.get('evidence_ref','')]; assert len(matches)==1,matches; assert matches[0]['signal_value']==0,matches\nprint(json.dumps({'run':r['run_id'],'status':r['status'],'digest':r['digests']['receipt_sha256'],'neutralCapture':True}))`.replace('PLACEHOLDER',run);
  const r=await execute(contract.toolPython(privateEnv),['-I','-c',code]);record('canonical-canceled-receipt',r);assert.equal(r.exit,0,r.stderr);
}
async function main() {
  const seen=new Set();
  const valued=new Set(['--artifacts','--out','--expected-platform','--registry-spec','--expected-packaging-sha','--version-check-python']);
  for(let i=0;i<args.length;i++) {
    const name=args[i];
    assert.ok(valued.has(name) || name==='--contained-linux-uncatchable','Unknown option: '+name);
    assert.ok(!seen.has(name),'Duplicate option: '+name);seen.add(name);
    if(valued.has(name)) {assert.ok(args[i+1] && !args[i+1].startsWith('--'),'Required: '+name);i++;}
  }
  if(args.includes('--version-check-python')) {
    assert.equal(args.length,6,'version-check-only mode accepts only --artifacts, --out and --version-check-python');
    receipt.acceptanceMode='version-check-only';
    const scratch=path.join(root,'identity-home');fs.mkdirSync(scratch,{recursive:true});
    const identityEnv={...process.env,HOME:scratch,USERPROFILE:scratch,XDG_CACHE_HOME:path.join(scratch,'cache')};
    await proveVersionRefresh(option('--version-check-python'),identityEnv,scratch);
    receipt.status='passed';receipt.completedAt=new Date().toISOString();save();
    console.log(JSON.stringify({status:receipt.status,receipt:reportPath,mode:receipt.acceptanceMode}));return;
  }
  const packagingIndex=args.indexOf('--expected-packaging-sha');
  const expectedPackagingSha=packagingIndex<0?process.env.GITHUB_SHA:args[packagingIndex+1];
  if(packagingIndex>=0) {
    assert.ok(registryMode,'--expected-packaging-sha requires --registry-spec');
    assert.match(expectedPackagingSha,/^[a-f0-9]{40}$/,'--expected-packaging-sha must be 40 lowercase hex');
  }
  if(registryMode) {
    assert.match(process.env.GITHUB_SHA || '',/^[a-f0-9]{40}$/,'GITHUB_SHA must identify the validation commit');
    receipt.validationSha=process.env.GITHUB_SHA;
    receipt.packagingSha=expectedPackagingSha;
  }
  setup();
  const metadata=JSON.parse(fs.readFileSync(path.join(artifactsDir,'artifacts.json'),'utf8'));
  receipt.sourceSha256=metadata.sourceSha256;
  assert.ok(receipt.sourceSha256,'packed artifacts must identify final source');
  runtime=metadata.runtime;
  const launcherArtifact=metadata.artifacts.find(a=>!a.platform);
  if(registryMode) assert.equal(registrySpec,`${launcherArtifact.name}@${runtime.version}`,'registry spec must pin reviewed launcher');
  const platform=receipt.platform;
  const chosen=metadata.artifacts.filter(a=>a.platform===platform || !a.platform);
  assert.equal(chosen.length,2,'launcher plus compatible companion required');
  for(const artifact of metadata.artifacts) {
    const data=fs.readFileSync(path.join(artifactsDir,artifact.filename));
    const sha=crypto.createHash('sha256').update(data).digest('hex');
    assert.equal(sha,artifact.sha256);assert.equal(data.length,artifact.bytes);
    assert.equal('sha512-'+crypto.createHash('sha512').update(data).digest('base64'),artifact.integrity);
    assert.equal(crypto.createHash('sha1').update(data).digest('hex'),artifact.shasum);
    receipt.artifacts.push({filename:artifact.filename,sha256:sha,bytes:data.length,unpackedBytes:artifact.unpackedBytes,installed:chosen.includes(artifact)});
  }
  assert.equal(metadata.artifacts.length,6);
  receipt.source={sourceCommit:metadata.sourceCommit,sourceTree:metadata.sourceTree,sourceIdentity:metadata.sourceIdentity,buildTools:metadata.buildTools};
  for(const artifact of metadata.artifacts) {
    const files=tarFiles(fs.readFileSync(path.join(artifactsDir,artifact.filename)));
    const manifest=JSON.parse(files.get('package/package.json'));
    assert.equal(manifest.private,undefined);assert.equal(manifest.version,metadata.runtime.version);
    assert.equal(manifest.publishConfig.access,'public');
    assert.deepEqual(manifest.repository,{type:'git',url:'git+https://github.com/escoffier-labs/brigade.git',directory:'npm/feasibility'});
    if(!artifact.platform) assert.deepEqual(manifest.optionalDependencies,Object.fromEntries(Object.values(metadata.runtime.platforms).map(p=>[p.package,metadata.runtime.version])));
    if(!artifact.platform) {
      const packed=JSON.parse(files.get('package/runtime.json'));
      assert.deepEqual(packed,metadata.runtime);assert.equal(packed.payload.version,metadata.runtime.payloadVersion);
      assert.match(packed.payload.sourceCommit,/^[a-f0-9]{40}$/);
      const wheel=files.get('package/wheel/'+packed.payload.wheel);
      assert.equal(hash(wheel),packed.payload.sha256);assert.equal(wheel.length,packed.payload.bytes);
      assert.equal([...files.keys()].filter(n=>n.startsWith('package/wheel/')).length,1);
    }
  }
  if(registryMode) {
    assert.equal(metadata.sourceCommit,expectedPackagingSha,'artifact packaging commit must equal expected packaging SHA (default GITHUB_SHA)');
    const verifier=require('./provenance.cjs').createVerifier(npmCli,path.join(root,'provenance-tuf'));
    for(const artifact of metadata.artifacts) {
      let visible=false;
      for(const delay of [0,1000,2000,4000,8000]) {
        if(delay) await new Promise(resolve=>setTimeout(resolve,delay));
        const r=await execute(process.execPath,[npmCli,'view',`${artifact.name}@${runtime.version}`,'dist.integrity','--json']);
        if(r.exit===0 && r.stdout.trim()) {assert.equal(JSON.parse(r.stdout),artifact.integrity);visible=true;break;}
      }
      assert.ok(visible,'registry visibility deadline: '+artifact.name);
      let verified=false;
      for(const delay of [0,1000,2000,4000,8000]) {
        if(delay) await new Promise(resolve=>setTimeout(resolve,delay));
        const attestation=await execute(process.execPath,[npmCli,'view',`${artifact.name}@${runtime.version}`,'dist.attestations','--json']);
        if(attestation.exit!==0 || !attestation.stdout.trim()) continue;
        const url=JSON.parse(attestation.stdout)?.url;
        if(!url) continue;
        const digest=crypto.createHash('sha512').update(fs.readFileSync(path.join(artifactsDir,artifact.filename))).digest('hex');
        try {
          if(await verifier.verifyRegistry(url,digest,{
            purl:verifier.purl(artifact.name,runtime.version),sourceCommit:metadata.sourceCommit,
            repository:'https://github.com/escoffier-labs/brigade',workflow:'.github/workflows/npm-publish.yml',ref:'refs/heads/main',
            san:'https://github.com/escoffier-labs/brigade/.github/workflows/npm-publish.yml@refs/heads/main',
            issuer:'https://token.actions.githubusercontent.com',builder:'https://github.com/actions/runner/github-hosted',
          })) {verified=true;break;}
        } catch(error) {throw new Error(`Invalid registry provenance: ${artifact.name}: ${error.message}`,{cause:error});}
      }
      assert.ok(verified,'registry provenance deadline: '+artifact.name);
    }
    const latest=await execute(process.execPath,[npmCli,'view',launcherArtifact.name,'dist-tags.latest','--json']);
    assert.equal(latest.exit,0);assert.equal(JSON.parse(latest.stdout),runtime.version);
  }
  npxEnv={...env,HOME:path.join(root,'npx-home'),USERPROFILE:path.join(root,'npx-home'),BRIGADE_NPM_CACHE:path.join(root,'npx-state'),npm_config_cache:path.join(root,'npx-cache')};
  fs.mkdirSync(npxEnv.HOME);npxPackages=chosen.flatMap(a=>['--package',path.join(artifactsDir,a.filename)]);
  receipt.npxFresh={prefixExists:fs.existsSync(path.join(prefix,'node_modules')),cacheExists:fs.existsSync(npxEnv.npm_config_cache),home:npxEnv.HOME};
  assert.equal(receipt.npxFresh.prefixExists,false);assert.equal(receipt.npxFresh.cacheExists,false);
  const npxCold=await npxCall('npx-cold-version',['--version']);versionOK(npxCold);
  assert.equal((await npxCall('npx-help',['--help'])).exit,0);
  const npxInvalid=await npxCall('npx-nonzero',['not-a-command']);assert.equal(npxInvalid.exit,2);
  save();
  const install=await execute(process.execPath,[npmCli,'install',...(registryMode?[]:['--offline']),'--ignore-scripts','--no-audit','--no-fund','--prefix',prefix,...(registryMode?[registrySpec]:chosen.map(a=>path.join(artifactsDir,a.filename)))]);
  record('install-local-tarballs',install);assert.equal(install.exit,0,install.stderr);
  launcher=path.join(prefix,'node_modules',...launcherArtifact.name.split('/'),'launcher.cjs');
  if(registryMode) {
    function verifyLock(file) {
      const lock=JSON.parse(fs.readFileSync(file));
      for(const a of chosen) {
        const installed=lock.packages['node_modules/'+a.name];
        assert.ok(installed,'optional companion must resolve from registry: '+a.name);
        assert.equal(installed.integrity,a.integrity);assert.equal(installed.version,runtime.version);
      }
      record('registry-lockfile-integrity',{file,packages:chosen.map(a=>({name:a.name,integrity:a.integrity}))});
    }
    verifyLock(path.join(prefix,'package-lock.json'));
    const npxInstalls=fs.readdirSync(path.join(npxEnv.npm_config_cache,'_npx'));
    assert.equal(npxInstalls.length,1);verifyLock(path.join(npxEnv.npm_config_cache,'_npx',npxInstalls[0],'package-lock.json'));
    const signatures=await execute(process.execPath,[npmCli,'audit','signatures'],{cwd:prefix});
    record('npm-audit-signatures',signatures);assert.equal(signatures.exit,0,signatures.stderr);
  }
  contract=require(launcher);
  runtime=require(path.join(path.dirname(launcher),'runtime.json'));
  uv=contract.resolveRuntime(runtime);
  receipt.payload=runtime.payload;
  receipt.runtime={uvVersion:runtime.uvVersion,pythonVersion:runtime.pythonVersion,pythonBuild:runtime.pythonBuild,brigadeVersion:runtime.version};
  receipt.pathExcludesPythonAndUv=true;
  receipt.path=env.PATH;
  receipt.pathEntries=env.PATH.split(path.delimiter).flatMap(dir=>fs.readdirSync(dir)).filter(name=>/^(?:pythonw?(?:\d+(?:\.\d+)*[dmtu]*)?(?:_d)?|pyw?(?:_d)?|uv[wx]?)(?:\.(?:exe|com|cmd|bat|ps1))?$/i.test(name));
  assert.deepEqual(receipt.pathEntries,[],'PATH must exclude Python, py.exe and uv');
  const cold=await call('cold-version',['--version']);versionOK(cold);
  const toolReceipt=path.join(contract.runtimeEnvironment(env,runtime).UV_TOOL_DIR,'brigade-cli','uv-receipt.toml');
  const installedMtime=fs.statSync(toolReceipt).mtimeMs;
  const warm=await call('warm-version',['--version']);versionOK(warm);assert.equal(cold.stdout,warm.stdout,'bootstrap stdout must be byte identical');assert.equal(fs.statSync(toolReceipt).mtimeMs,installedMtime);assert.equal(warm.stderr,'');
  assert.equal(fs.statSync(toolReceipt).mtimeMs,installedMtime,'warm startup must not rewrite installation receipt');assert.equal(warm.stderr,'','warm bootstrap should be quiet');
  // Archive/install boundary: the packed launcher must share alias/canonical state.
  const aliasReal=path.join(root,'alias-real'),aliasParent=path.join(root,'alias-parent');
  fs.mkdirSync(aliasReal);fs.symlinkSync(aliasReal,aliasParent,process.platform==='win32'?'junction':'dir');
  const aliasResults=[];let aliasMarker,aliasMarkerBytes,aliasInstallTime;
  for(const cache of [path.join(aliasParent,'state'),path.join(aliasReal,'state'),path.join(aliasParent,'state')]) {
    const result=await execute(process.execPath,[launcher,'--version'],{env:{...env,BRIGADE_NPM_CACHE:cache}});
    aliasResults.push(result);versionOK(result);
    const selected=contract.runtimeEnvironment({...env,BRIGADE_NPM_CACHE:cache},runtime);
    const marker=path.join(path.dirname(selected.UV_TOOL_DIR),'initialized.json');
    const installed=path.join(selected.UV_TOOL_DIR,'brigade-cli','uv-receipt.toml');
    if(!aliasMarkerBytes) {aliasMarker=fs.realpathSync(marker);aliasMarkerBytes=fs.readFileSync(marker);aliasInstallTime=fs.statSync(installed).mtimeMs;}
    else {assert.equal(fs.realpathSync(marker),aliasMarker);assert.deepEqual(fs.readFileSync(marker),aliasMarkerBytes);assert.equal(fs.statSync(installed).mtimeMs,aliasInstallTime);assert.equal(result.stderr,'');}
  }
  record('packed-cache-alias-reuse',{results:aliasResults,marker:aliasMarker,markerSha256:hash(aliasMarkerBytes),markerByteStable:true,installationReceiptStable:true});
  assert.equal((await call('help',['--help'])).exit,0);
  const npmBin=await execute(process.execPath,[npmCli,'exec','--offline','--prefix',prefix,'--','brigade','--version']);
  record('installed-npm-bin-version',npmBin);versionOK(npmBin);
  const privateEnv=contract.runtimeEnvironment(env,runtime);
  const pythonDirs=fs.readdirSync(privateEnv.UV_PYTHON_INSTALL_DIR).filter(n=>n.startsWith('cpython-'+runtime.pythonVersion+'-') && fs.lstatSync(path.join(privateEnv.UV_PYTHON_INSTALL_DIR,n)).isDirectory());
  assert.equal(pythonDirs.length,1,'one exact managed interpreter expected');
  const python=path.join(privateEnv.UV_PYTHON_INSTALL_DIR,pythonDirs[0],process.platform==='win32'?'python.exe':'bin/python3.12');
  const probe='import json,sys; print(json.dumps({"executable":sys.executable,"version":sys.version,"baseExecutable":sys._base_executable}))';
  const managed=await execute(python,['-I','-c',probe],{env:privateEnv});record('managed-interpreter',managed);
  assert.equal(managed.exit,0,managed.stderr);
  receipt.managedPython=JSON.parse(managed.stdout);
  assert.ok(fs.realpathSync(receipt.managedPython.executable).startsWith(fs.realpathSync(privateEnv.UV_PYTHON_INSTALL_DIR)+path.sep));
  receipt.managedBuild=fs.readFileSync(path.join(privateEnv.UV_PYTHON_INSTALL_DIR,pythonDirs[0],'BUILD'),'utf8').trim();
  assert.equal(receipt.managedBuild,runtime.pythonBuild,'uv installed BUILD metadata must prove exact build');
  assert.match(receipt.managedPython.version,new RegExp('^'+runtime.pythonVersion.replaceAll('.','\\.')));
  receipt.privateToolPython=contract.toolPython(privateEnv);
  const tool=await execute(contract.toolPython(privateEnv),['-I','-c',probe],{env});record('tool-interpreter',tool);assert.equal(tool.exit,0,tool.stderr);
  receipt.toolPython=JSON.parse(tool.stdout);
  assert.ok(fs.realpathSync(receipt.toolPython.baseExecutable).startsWith(fs.realpathSync(path.join(privateEnv.UV_PYTHON_INSTALL_DIR,pythonDirs[0]))+path.sep));
  await proveInstalledWheel(privateEnv);
  await proveIdentity(privateEnv,python);
  versionOK(await call('offline-warm',['--version'],{env:{...env,UV_OFFLINE:'true'}}));
  fs.rmSync(privateEnv.UV_TOOL_DIR,{recursive:true,force:true});
  versionOK(await call('offline-cached-python-reinstall',['--version'],{env:{...env,UV_OFFLINE:'true'}}));
  await proveInstalledWheel(privateEnv);
  await tamperIdentity(privateEnv);
  const offline=await call('offline-cold',['--version'],{env:{...env,UV_OFFLINE:'true',BRIGADE_NPM_CACHE:path.join(root,'offline-state')}});
  assert.notEqual(offline.exit,0);assert.match(offline.stderr,/offline|network.*disabled/i);
  versionOK(await call('offline-online-recovery',['--version'],{env:{...env,BRIGADE_NPM_CACHE:path.join(root,'offline-state')}}));
  const denied=await call('python-downloads-disabled-cold',['--version'],{env:{...env,UV_PYTHON_DOWNLOADS:'never',BRIGADE_NPM_CACHE:path.join(root,'no-download-state')}});assert.notEqual(denied.exit,0);assert.match(denied.stderr,/download|managed.*python/i);
  const concurrentEnv={...env,BRIGADE_NPM_CACHE:path.join(root,'concurrent-state')};
  const concurrent=await Promise.all([call('concurrent-first-1',['--version'],{env:concurrentEnv}),call('concurrent-first-2',['--version'],{env:concurrentEnv})]);
  concurrent.forEach(versionOK);
  const interruptedEnv={...env,BRIGADE_NPM_CACHE:path.join(root,'interrupted-state')};
  let interruptScheduled=false;
  let observedDownload=false,launcherSignalPhase=null;
  const interrupted=await call('interrupted-initialization',['--version'],{env:interruptedEnv,onOutput:(child,stdout,stderr)=>{
    if(!interruptScheduled && /Downloading cpython/i.test(stderr)) {
      observedDownload=true;interruptScheduled=true;
      launcherSignalPhase=/Downloaded cpython/i.test(stderr)?'provisioning after Python download':'Python download progress observed';
      terminateTree(child);
    }
  }});
  record('interrupted-state',{bytes:size(interruptedEnv.BRIGADE_NPM_CACHE),observedDownload,launcherSignalPhase});
  assert.ok(observedDownload,'must observe provisioning before interruption');assert.ok(interrupted.signal || interrupted.exit!==0,'must interrupt before successful initialization');
  versionOK(await call('interrupted-recovery',['--version'],{env:interruptedEnv}));
  const target=path.join(root,process.platform==='win32'?'git target quoted':'git target \'quoted"');
  const git=spawnSync('git',['-c',`init.templateDir=${gitTemplate}`,'init','-q','-b','main',target],{encoding:'utf8',env,cwd:root});
  assert.equal(git.status,0,git.stderr);
  const init=await call('init-quoted-target',['init','--target',target,'--depth','repo','--harnesses','codex']);
  assert.equal(init.exit,0,init.stderr);assert.ok(fs.existsSync(path.join(target,'.brigade')));
  const doctor=await call('doctor',['doctor','--target',target]);
  receipt.doctor={exit:doctor.exit,signal:doctor.signal,stdout:doctor.stdout,stderr:doctor.stderr};
  // Preserve a published-core failure and finish independent checks before reporting it.
  const invalid=await call('argv-nonzero',['not-a-command',`space quote'"literal`]);assert.equal(invalid.exit,2);assert.match(invalid.stderr,/not-a-command/);
  fs.writeFileSync(path.join(root,'uv.toml'),'invalid and deliberately unusable uv configuration');
  versionOK(await call('environment-isolation',['--version'],{env:{...env,UV_PYTHON:'system',UV_INDEX_URL:'https://invalid.example/simple',UV_CACHE_DIR:path.join(root,'unwanted'),UV_CONFIG_FILE:path.join(root,'missing.toml'),PYTHONPATH:path.join(root,'unwanted')}}));
  assert.ok(!fs.existsSync(path.join(root,'unwanted')));
  const selected={UV_CACHE_DIR:path.join(root,'caller-uv-cache'),UV_TOOL_DIR:path.join(root,'caller-uv-tools'),UV_PYTHON:'caller-choice',UV_INDEX_URL:'https://caller.invalid/simple',PATH:env.PATH,PYTHONPATH:'/fake-caller-python',VIRTUAL_ENV:'/fake-caller-venv',BRIGADE_DISTRIBUTION:'caller-value',BRIGADE_NO_UPDATE_CHECK:'caller-opt-out'};
  const childCode=`console.log(JSON.stringify(Object.fromEntries(${JSON.stringify(Object.keys(selected))}.map(k=>[k,process.env[k]]))))`;
  const envProbe=await call('verification-caller-environment',['work','verify','run','--target',target,'--argv-json',JSON.stringify([process.execPath,'-e',childCode])],{env:{...env,...selected}});
  assert.equal(envProbe.exit,0,envProbe.stderr);
  const verifyDirs=fs.readdirSync(path.join(target,'.brigade/work/verify-runs')).sort();
  const observed=JSON.parse(fs.readFileSync(path.join(target,'.brigade/work/verify-runs',verifyDirs.at(-1),'command-1-stdout.log'),'utf8'));
  record('verification-caller-environment-observed',{selected:observed});assert.deepEqual(observed,selected);
  const absentCode="console.log(JSON.stringify(Object.fromEntries(['BRIGADE_DISTRIBUTION','BRIGADE_NO_UPDATE_CHECK'].map(k=>[k,{present:Object.hasOwn(process.env,k),value:process.env[k]??null}]))))";
  const beforeAbsent=new Set(fs.readdirSync(path.join(target,'.brigade/work/verify-runs')));
  const absent=await call('verification-caller-markers-absent',['work','verify','run','--target',target,'--argv-json',JSON.stringify([process.execPath,'-e',absentCode])]);assert.equal(absent.exit,0,absent.stderr);
  const absentDir=fs.readdirSync(path.join(target,'.brigade/work/verify-runs')).find(d=>!beforeAbsent.has(d));assert.ok(absentDir);
  const absentObserved=JSON.parse(fs.readFileSync(path.join(target,'.brigade/work/verify-runs',absentDir,'command-1-stdout.log'),'utf8'));
  assert.deepEqual(absentObserved,{BRIGADE_DISTRIBUTION:{present:false,value:null},BRIGADE_NO_UPDATE_CHECK:{present:false,value:null}});
  record('verification-caller-markers-absent-observed',absentObserved);
  const quoteArgs=['space value',`quote'"value`,'%literal%', '^caret', '&amp', '|pipe', '<in>', '$(literal)'];
  const quoteCode='console.log(JSON.stringify(process.argv.slice(1))); process.exit(23)';
  const quoteCommand=['work','verify','run','--target',target,'--argv-json',JSON.stringify([process.execPath,'-e',quoteCode,...quoteArgs])];
  const beforeDirect=new Set(fs.readdirSync(path.join(target,'.brigade/work/verify-runs')));
  const directQuote=await call('argv-quoting-direct',quoteCommand);assert.notEqual(directQuote.exit,0);
  const directDir=fs.readdirSync(path.join(target,'.brigade/work/verify-runs')).find(d=>!beforeDirect.has(d));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(target,'.brigade/work/verify-runs',directDir,'command-1-stdout.log'),'utf8')),quoteArgs);
  const beforeShim=new Set(fs.readdirSync(path.join(target,'.brigade/work/verify-runs')));
  const shimQuote=await execute(process.execPath,[npmCli,'exec','--offline','--prefix',prefix,'--','brigade',...quoteCommand]);record('argv-quoting-npm-command',shimQuote);
  const shimDir=fs.readdirSync(path.join(target,'.brigade/work/verify-runs')).find(d=>!beforeShim.has(d));
  let shimPreserved=false;
  if(shimDir) {try {shimPreserved=JSON.stringify(JSON.parse(fs.readFileSync(path.join(target,'.brigade/work/verify-runs',shimDir,'command-1-stdout.log'),'utf8')))===JSON.stringify(quoteArgs);} catch {}}
  record('npm-command-quoting-result',{preserved:shimPreserved});
  if(process.platform==='win32' && !shimPreserved) receipt.limitations.push('Installed npm command shim did not preserve metacharacter arguments. Direct Node launcher transport passed separately.');
  else assert.ok(shimPreserved,'installed npm command must preserve arguments on POSIX');
  const beforeNpx=new Set(fs.readdirSync(path.join(target,'.brigade/work/verify-runs')));
  const npxQuote=await npxCall('npx-argv-nonzero',quoteCommand);assert.notEqual(npxQuote.exit,0);
  const npxDir=fs.readdirSync(path.join(target,'.brigade/work/verify-runs')).find(d=>!beforeNpx.has(d));
  assert.ok(npxDir);assert.deepEqual(JSON.parse(fs.readFileSync(path.join(target,'.brigade/work/verify-runs',npxDir,'command-1-stdout.log'),'utf8')),quoteArgs);
  // Real verification starts a detached grandchild. Catchable signals must commit.
  const cancellationResults=[];
  const cancellationEnv={...env,BRIGADE_HOME:path.join(root,'cancel-fleet'),BRIGADE_FLEET_HUB_URL:'offline://candidate'};
  receipt.eventObservation={transport:'unsupported offline URL scheme, no credentials or network',spool:path.join(cancellationEnv.BRIGADE_HOME,'fleet-spool','unknown.jsonl')};
  const modes=process.platform==='win32'?['windows-external-tree-termination']:
    ['SIGINT','SIGTERM','SIGHUP'].flatMap(signal=>['pid','group'].map(scope=>({signal,scope}))).concat(containedUncatchable?[{signal:'SIGKILL',scope:'group'}]:[]);
  receipt.cancellationModes=modes;save();
  if(process.platform==='win32') receipt.limitations.push('Windows console Ctrl+C remains unproved. Forced taskkill tree termination is separate.');
  for(const item of modes) {
    const mode=typeof item==='string'?item:`posix-${item.scope}-${item.signal}`;
    const ready=path.join(root,'cancel-ready-'+mode);
    const detached=path.join(root,'detached-'+mode);
    const grandCode=`import os,time,json; from pathlib import Path; Path(${JSON.stringify(detached)}).write_text(json.dumps({'pid':os.getpid()})); time.sleep(120)`;
    const code=`import os,time,json,subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-I','-c',${JSON.stringify(grandCode)}],start_new_session=${process.platform==='win32'?'False':'True'});\nwhile not Path(${JSON.stringify(detached)}).exists(): time.sleep(0.01)\nPath(${JSON.stringify(ready)}).write_text(json.dumps({'pid':os.getpid(),'grandchildPid':p.pid,'applicationPid':os.getppid(),'applicationGroup':os.getpgid(os.getppid()) if hasattr(os,'getpgid') else None})); time.sleep(120)`;
    let identities,launcherPid,cancellationSent=false;
    const before=new Set(fs.readdirSync(path.join(target,'.brigade/work/verify-runs')));
    const cancelled=await call(mode,['work','verify','run','--target',target,'--capture','brigade-work','--argv-json',JSON.stringify([python,'-I','-c',code])],{env:cancellationEnv,timeout:30000,processGroup:item.scope==='group',onSpawn:(child,done)=>{
      const poll=setInterval(()=>{
        if(done()) {clearInterval(poll);return;}
        if(!fs.existsSync(ready)) return;
        let data;try {data=JSON.parse(fs.readFileSync(ready,'utf8'));} catch {return;}
        if(![data.pid,data.grandchildPid,data.applicationPid].every(p=>Number.isInteger(p)&&p>1)) return;
        clearInterval(poll);identities=[data.pid,data.grandchildPid,data.applicationPid].map(identityOf);launcherPid=child.pid;
        assert.ok(identities.every(Boolean),'pin every test identity before signaling');
        cancellationSent=true;
        if(item.scope==='group') {assert.equal(data.applicationGroup,child.pid);process.kill(-child.pid,item.signal);}
        else if(process.platform==='win32') terminateTree(child);
        else child.kill(item.signal);
      },25);
    }});
    assert.ok(cancellationSent);assert.ok(cancelled.signal || cancelled.exit!==0);
    await new Promise(resolve=>setTimeout(resolve,250));
    const survivors=identities.filter(identityAlive);
    const caught=process.platform!=='win32' && item.signal!=='SIGKILL';
    const dirs=fs.readdirSync(path.join(target,'.brigade/work/verify-runs')).filter(d=>!before.has(d));
    record('cancellation-process-proof',{mode,identities,survivors,launcherPid,ms:cancelled.ms});
    try {
      if(caught) {
        assert.deepEqual(survivors,[],'catchable signal left descendants');assert.ok(cancelled.ms<20000,'must finish before force');
        assert.equal(dirs.length,1);await proveCanceledReceipt(target,dirs[0]);
        const r=JSON.parse(fs.readFileSync(path.join(target,'.brigade/work/verify-runs',dirs[0],'receipt.json')));
        const events=fs.readFileSync(receipt.eventObservation.spool,'utf8').trim().split('\n').filter(Boolean).map(line=>JSON.parse(line)).filter(event=>event.run_id===r.run_id);
        assert.equal(events.length,1,'one corresponding fleet event required');assert.equal(events[0].state,'verify.completed');assert.equal(events[0].digest,r.digests.receipt_sha256);assert.equal(cancelled.exit,130,'canonical canceled CLI status');assert.equal(events[0].exit_status,cancelled.exit);
        assert.deepEqual(r.interruption,item.signal==='SIGINT'?{kind:'keyboard-interrupt',detail:'verification canceled by user'}:{kind:'signal',signal:item.signal,detail:'verification canceled by '+item.signal});
        record('cancellation-one-event',{mode,event:events[0]});
      } else if(process.platform==='win32') {
        assert.deepEqual(survivors,[],'Windows forced tree termination left descendants');
      } else {
        receipt.limitations.push(`${mode}: uncatchable termination can leave detached descendants and no receipt. Test identities are explicitly cleaned.`);
      }
      cancellationResults.push({mode,caught,survivors:survivors.length});
    } finally {
      for(const identity of survivors) if(identityAlive(identity)) process.kill(identity.pid,'SIGKILL');
      await new Promise(resolve=>setTimeout(resolve,250));
      record('cancellation-final-no-survivors',{mode,identities,survivors:identities.filter(identityAlive)});
      assert.deepEqual(identities.filter(identityAlive),[]);
    }
  }
  receipt.cancellation=cancellationResults;
  const omittedPrefix=path.join(root,'omitted-prefix');
  const omittedInstall=await execute(process.execPath,[npmCli,'install',...(registryMode?[]:['--offline']),'--ignore-scripts','--omit=optional','--prefix',omittedPrefix,registryMode?registrySpec:path.join(artifactsDir,launcherArtifact.filename)]);
  record('launcher-only-omit-optional-install',omittedInstall);assert.equal(omittedInstall.exit,0,omittedInstall.stderr);
  const omitted=await execute(process.execPath,[path.join(omittedPrefix,'node_modules',...launcherArtifact.name.split('/'),'launcher.cjs'),'--version']);
  record('launcher-only-omit-optional-start',omitted);assert.equal(omitted.exit,1);assert.match(omitted.stderr,/--omit=optional/);
  const pin=runtime.platforms[platform];
  const binary=uv;
  const original=fs.readFileSync(binary);
  try {
    fs.appendFileSync(binary,'corruption');
    const corrupt=await call('corrupt-companion',['--version']);assert.equal(corrupt.exit,1);assert.match(corrupt.stderr,/integrity/i);
  } finally {fs.writeFileSync(binary,original);}
  const manifestPath=path.join(path.dirname(binary),'manifest.json');
  const hidden=manifestPath+'.hidden';
  fs.renameSync(manifestPath,hidden);
  try { const missing=await call('missing-companion',['--version']);assert.equal(missing.exit,1);assert.match(missing.stderr,/--omit=optional/); }
  finally {fs.renameSync(hidden,manifestPath);}
  receipt.state={warm:usage(env.BRIGADE_NPM_CACHE),concurrent:usage(concurrentEnv.BRIGADE_NPM_CACHE),recovered:usage(interruptedEnv.BRIGADE_NPM_CACHE),totalSandbox:usage(root)};
  assert.equal(doctor.exit,0,doctor.stdout+'\n'+doctor.stderr);
  assert.ok(cancellationResults.filter(r=>r.caught).every(r=>r.survivors===0));
  receipt.cancellationGate='passed';
  receipt.status='passed';receipt.completedAt=new Date().toISOString();save();
  console.log(JSON.stringify({status:receipt.status,receipt:reportPath,root,coldMs:cold.ms,warmMs:receipt.checks.find(c=>c.name==='warm-version').ms,state:receipt.state,managedPython:receipt.managedPython,companion:pin.package},null,2));
}
main().catch(error=>{receipt.status='failed';receipt.error=redact(error.stack || error);receipt.completedAt=new Date().toISOString();save();console.error(`Acceptance failed. Receipt: ${reportPath}\n${receipt.error}`);process.exitCode=1;});
