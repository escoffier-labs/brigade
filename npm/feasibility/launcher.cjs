#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');

// Exported for artifact acceptance to use the exact production environment contract.
const networkControls = new Set([
  'UV_OFFLINE', 'UV_PYTHON_DOWNLOADS', 'UV_NATIVE_TLS', 'UV_SYSTEM_CERTS',
  'UV_HTTP_TIMEOUT', 'UV_HTTP_CONNECT_TIMEOUT', 'UV_HTTP_RETRIES',
]);
function runtimeEnvironment(parent, runtime) {
  const env = { ...parent };
  for (const key of Object.keys(env)) {
    const upper = key.toUpperCase();
    if ((upper.startsWith('UV_') && !networkControls.has(upper)) ||
        ['VIRTUAL_ENV','CONDA_PREFIX','PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','PYTHONUSERBASE'].includes(upper)) delete env[key];
  }
  const root = path.resolve(parent.BRIGADE_NPM_CACHE || path.join(os.homedir(), '.cache', 'brigade-npm'));
  const toolRoot=path.join(root,'payloads',runtime.payload.sha256);
  const pythonRoot=path.join(root,'python',`${runtime.pythonVersion}-${runtime.pythonBuild}`);
  Object.assign(env, {
    UV_CACHE_DIR:path.join(toolRoot,'uv-cache'), UV_TOOL_DIR:path.join(toolRoot,'tools'),
    UV_TOOL_BIN_DIR:path.join(toolRoot,'tools','brigade-cli','.npm-bin'), UV_PYTHON_INSTALL_DIR:path.join(pythonRoot,'install'),
    UV_PYTHON_BIN_DIR:path.join(pythonRoot,'bin'), UV_PYTHON_CACHE_DIR:path.join(pythonRoot,'cache'),
    UV_MANAGED_PYTHON:'true', UV_PYTHON_CPYTHON_BUILD:runtime.pythonBuild,
    UV_NO_CONFIG:'true', UV_NO_ENV_FILE:'true', UV_PYTHON_INSTALL_BIN:'false',
    PYTHONNOUSERSITE:'1',
  });
  // Python mirrors require explicit opt-in, never inherited UV_*.
  if (parent.BRIGADE_NPM_PYTHON_MIRROR) env.UV_PYTHON_INSTALL_MIRROR=parent.BRIGADE_NPM_PYTHON_MIRROR;
  return env;
}
function resolveRuntime(runtime) {
  const key = `${process.platform}-${process.arch}`;
  const platform = runtime.platforms[key];
  process.report.excludeNetwork=true;
  if (!platform || (process.platform==='linux' && !process.report.getReport().header.glibcVersionRuntime)) {
    throw new Error(`Unsupported platform ${key}. Use Python 3.10+ and pipx install brigade-cli instead.`);
  }
  let manifestPath;
  try { manifestPath=require.resolve(`${platform.package}/manifest.json`); }
  catch { throw new Error(`Missing optional runtime ${platform.package}@${runtime.version}. Reinstall with optional dependencies enabled. --omit=optional prevents Brigade startup.`); }
  const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8'));
  if (manifest.version!==runtime.version || manifest.uvVersion!==runtime.uvVersion ||
      manifest.executable!==platform.executable || manifest.sha256!==platform.binarySha256) {
    throw new Error('Companion integrity manifest mismatch. Reinstall the exact package version.');
  }
  const binary=path.join(path.dirname(manifestPath),platform.executable);
  try {
    if (!fs.lstatSync(binary).isFile()) throw new Error('Companion integrity: expected a regular executable.');
  } catch(error) {
    if(error.code==='ENOENT') throw new Error('Companion executable missing. Reinstall the exact package version with optional dependencies enabled.');
    throw error;
  }
  const hash=crypto.createHash('sha256').update(fs.readFileSync(binary)).digest('hex');
  if (hash!==platform.binarySha256) throw new Error('Companion integrity SHA-256 mismatch. Reinstall the exact package version.');
  return binary;
}
function payloadWheel(runtime) {
  const p=runtime.payload;
  if(!p || !/^[a-f0-9]{64}$/.test(p.sha256) || !Number.isSafeInteger(p.bytes) || p.bytes<=0 ||
     p.distribution!=='brigade-cli' || p.version!==runtime.payloadVersion ||
     typeof p.wheel!=='string' || !/^brigade_cli-\d+\.\d+\.\d+(?:\.dev\d+)?-py3-none-any\.whl$/.test(p.wheel) ||
     p.wheel!==`brigade_cli-${runtime.payloadVersion}-py3-none-any.whl`) {
    throw new Error('Bundled wheel integrity manifest invalid. Reinstall the exact package version.');
  }
  const directory=path.join(__dirname,'wheel');
  const wheel=path.join(directory,p.wheel);
  if(!fs.lstatSync(directory).isDirectory() || fs.lstatSync(directory).isSymbolicLink() ||
     !fs.lstatSync(wheel).isFile() || fs.realpathSync(wheel)!==path.join(fs.realpathSync(directory),p.wheel) ||
     fs.statSync(wheel).size!==p.bytes || sha256(fs.readFileSync(wheel))!==p.sha256) {
    throw new Error('Bundled wheel integrity SHA-256 or size mismatch. Reinstall the exact package version.');
  }
  return wheel;
}
function sha256(bytes) {return crypto.createHash('sha256').update(bytes).digest('hex');}
const identityContent='import brigade.distribution as d; d._name = "npm"\n';
function inside(parent,child) {
  const relative=path.relative(parent,child);
  return relative!=='' && relative!=='..' && !relative.startsWith('..'+path.sep) && !path.isAbsolute(relative);
}
function atomicWrite(file,content) {
  const temporary=file+'.'+process.pid+'.'+crypto.randomBytes(8).toString('hex')+'.tmp';
  try {fs.writeFileSync(temporary,content,{flag:'wx',mode:0o600});fs.renameSync(temporary,file);}
  finally {fs.rmSync(temporary,{force:true});}
}
function markerPath(env) {return path.join(path.dirname(env.UV_TOOL_DIR),'initialized.json');}
function markerBase(runtime) {
  return {schema:1,wheelSha256:runtime.payload.sha256,pythonVersion:runtime.pythonVersion,pythonBuild:runtime.pythonBuild,uvVersion:runtime.uvVersion};
}
function validIdentity(identity,env) {
  const venv=fs.realpathSync(path.join(env.UV_TOOL_DIR,'brigade-cli'));
  return identity && typeof identity.path==='string' && inside(venv,identity.path) &&
    path.basename(identity.path)==='_brigade_npm.pth' && identity.sha256===sha256(identityContent) &&
    fs.lstatSync(identity.path).isFile() && fs.realpathSync(identity.path)===identity.path &&
    fs.readFileSync(identity.path,'utf8')===identityContent;
}
async function initialized(runtime,env,locate) {
  try {
    const marker=JSON.parse(fs.readFileSync(markerPath(env),'utf8'));
    if(!Object.entries(markerBase(runtime)).every(([k,v])=>marker[k]===v) ||
       !fs.existsSync(toolPython(env)) || !validIdentity(marker.identity,env)) return false;
    const purelib=await locate();
    return marker.identity.path===path.join(purelib,'_brigade_npm.pth');
  } catch {return false;}
}
function toolArguments(runtime) {
  return ['--no-config','tool','install','--managed-python','--python',runtime.pythonVersion,
    '--no-index',payloadWheel(runtime)];
}
function toolPython(env) {
  return path.join(env.UV_TOOL_DIR,'brigade-cli',process.platform==='win32'?'Scripts/python.exe':'bin/python');
}
async function main() {
  let child, phase, escalation, forwarding, cancelled=false,receivedSignal=null;
  const handlers={};
  function force() {
    if(!child || child.exitCode!==null || child.signalCode!==null) return;
    if(process.platform==='win32') {
      spawn(path.join(process.env.SystemRoot || 'C:\\Windows','System32','taskkill.exe'),
        ['/PID',String(child.pid),'/T','/F'],{stdio:'ignore',shell:false}).once('error',()=>child.kill('SIGKILL'));
    } else child.kill('SIGKILL');
  }
  function run(executable,argv,env,bootstrap=false,capture=false) {
    phase=bootstrap?'bootstrap':'application';
    return new Promise((resolve,reject)=>{
      child=spawn(executable,argv,{env,cwd:process.cwd(),stdio:capture?['ignore','pipe','inherit']:bootstrap?['inherit','ignore','inherit']:'inherit',shell:false});
      let stdout='';
      if(capture) child.stdout.on('data',data=>{stdout+=data; if(stdout.length>65536) child.kill();});
      child.once('error',reject);
      child.once(capture?'close':'exit',(code,signal)=>resolve({code,signal,stdout}));
    });
  }
  function finish(result) {
    // Normal exits, including handled cancellations, belong to the child.
    if(result.code!==null) process.exitCode=result.code;
    else {
      process.exitCode=128+(os.constants.signals[result.signal] || 1);
      if(process.platform!=='win32' && ['SIGINT','SIGTERM','SIGHUP'].includes(result.signal)) {
        process.kill(process.pid,result.signal);
      }
    }
  }
  try {
    const runtime=require('./runtime.json');
    const binary=resolveRuntime(runtime);
    const privateEnv=runtimeEnvironment(process.env,runtime);
    const root=path.resolve(process.env.BRIGADE_NPM_CACHE || path.join(os.homedir(),'.cache','brigade-npm'));
    fs.mkdirSync(root,{recursive:true,mode:0o700});
    const canonicalRoot=fs.realpathSync(root);
    const rootStat=fs.statSync(root);
    if(process.platform!=='win32' && (rootStat.uid!==process.getuid() || (rootStat.mode&0o022)!==0)) {
      throw new Error('Private cache root must be owned by the current user and have no group or other write permissions.');
    }
    function validateOwnedPath(directory) {
      const parts=path.relative(root,directory).split(path.sep);
      let lexical=root,canonical=canonicalRoot;
      for(const part of parts) {
        lexical=path.join(lexical,part);canonical=path.join(canonical,part);
        let entry;
        try {entry=fs.lstatSync(lexical);} catch(error) {if(error.code==='ENOENT') break;throw error;}
        if(!entry.isDirectory() || entry.isSymbolicLink() || fs.realpathSync(lexical)!==canonical) {
          throw new Error('Private tool environment must be an exact non-symlink directory.');
        }
      }
      return path.join(canonicalRoot,...parts);
    }
    function validateManagedPythonPaths() {
      for(const key of ['UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','UV_PYTHON_CACHE_DIR']) validateOwnedPath(privateEnv[key]);
    }
    validateManagedPythonPaths();
    validateOwnedPath(privateEnv.UV_CACHE_DIR);
    fs.mkdirSync(privateEnv.UV_CACHE_DIR,{recursive:true,mode:0o700});
    for(const signal of process.platform==='win32'?['SIGINT','SIGBREAK','SIGTERM','SIGHUP']:['SIGINT','SIGTERM','SIGHUP']) {
      handlers[signal]=()=>{
        if(cancelled) {
          if(process.platform!=='win32' && phase==='application') {
            clearTimeout(forwarding);
            if(child && child.exitCode===null && child.signalCode===null) child.kill(receivedSignal);
          } else force();
          return;
        }
        cancelled=true;receivedSignal=signal;
        // Attached Windows console events already reach the child. POSIX group
        // delivery also reaches it: allow that exit before forwarding a PID event.
        if(process.platform!=='win32' || !['SIGINT','SIGBREAK','SIGHUP'].includes(signal)) {
          forwarding=setTimeout(()=>{if(child && child.exitCode===null && child.signalCode===null) child.kill(signal);},1500);
        }
        escalation=setTimeout(force,20000);
      };
      process.on(signal,handlers[signal]);
    }
    payloadWheel(runtime); // Validate payload even on warm launches, before uv.
    async function locatePurelib() {
      const expectedVenv=validateOwnedPath(path.join(privateEnv.UV_TOOL_DIR,'brigade-cli'));
      if(!fs.lstatSync(expectedVenv).isDirectory() || fs.realpathSync(expectedVenv)!==expectedVenv) throw new Error('Private tool environment must be an exact non-symlink directory.');
      const located=await run(toolPython(privateEnv),['-I','-c',"import json,sys,sysconfig; print(json.dumps({'prefix':sys.prefix,'purelib':sysconfig.get_path('purelib')}))"],privateEnv,true,true);
      if(located.code!==0 || located.signal || cancelled) throw new Error('Private interpreter identity provisioning failed. No initialized marker was written.');
      const location=JSON.parse(located.stdout);
      const venv=fs.realpathSync(path.join(privateEnv.UV_TOOL_DIR,'brigade-cli'));
      const purelib=fs.realpathSync(location.purelib);
      if(fs.realpathSync(location.prefix)!==venv || !inside(venv,purelib) || !fs.statSync(purelib).isDirectory()) {
        throw new Error('Private interpreter purelib is outside the exact tool environment. Identity provisioning refused.');
      }
      return purelib;
    }
    const ready=await initialized(runtime,privateEnv,locatePurelib);
    if(cancelled) {
      for(const [signal,handler] of Object.entries(handlers)) process.removeListener(signal,handler);
      finish({code:null,signal:receivedSignal});return;
    }
    if(!ready) {
      const marker=markerPath(privateEnv);
      fs.rmSync(marker,{force:true});
      validateOwnedPath(path.join(privateEnv.UV_TOOL_DIR,'brigade-cli'));
      const installed=await run(binary,toolArguments(runtime),privateEnv,true);
      if(installed.code!==0 || installed.signal || cancelled) {
        if(installed.code!==0 && !cancelled) console.error('Brigade npm provisioning failed. The bundled wheel needs managed Python. Check offline/download restrictions, proxy settings and BRIGADE_NPM_PYTHON_MIRROR. No package index is used.');
        for(const [signal,handler] of Object.entries(handlers)) process.removeListener(signal,handler);
        finish(cancelled && installed.code===0?{code:null,signal:receivedSignal}:installed);return;
      }
      validateManagedPythonPaths();
      const purelib=await locatePurelib();
      const identity={path:path.join(purelib,'_brigade_npm.pth'),sha256:sha256(identityContent)};
      atomicWrite(identity.path,identityContent);
      if(!validIdentity(identity,privateEnv)) throw new Error('Private interpreter identity validation failed.');
      atomicWrite(marker,JSON.stringify({...markerBase(runtime),identity})+'\n');
    }
    validateManagedPythonPaths();
    const callerEnv={...process.env};
    const result=await run(toolPython(privateEnv),['-I','-c',"import sys; sys.argv[0]='brigade'; import brigade.distribution as d; d._name='npm'; from brigade.cli import main; sys.exit(main())",...process.argv.slice(2)],callerEnv);
    clearTimeout(escalation);clearTimeout(forwarding);
    for(const [signal,handler] of Object.entries(handlers)) process.removeListener(signal,handler);
    finish(result);
  } catch(error) {
    if(cancelled) {
      for(const [signal,handler] of Object.entries(handlers)) process.removeListener(signal,handler);
      finish({code:null,signal:receivedSignal});return;
    }
    console.error(`Brigade npm: ${error.message}${['EACCES','ENOENT'].includes(error.code)?'. Check executable permissions and reinstall the exact companion artifacts.':''}`);
    process.exitCode=1;
  } finally {
    clearTimeout(escalation);clearTimeout(forwarding);
    for(const [signal,handler] of Object.entries(handlers)) process.removeListener(signal,handler);
  }
}
module.exports={runtimeEnvironment,resolveRuntime,toolArguments,toolPython};
if (require.main===module) main();
