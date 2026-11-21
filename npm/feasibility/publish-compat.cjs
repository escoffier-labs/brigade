'use strict';
// Publish the @escoffier-labs/brigade compatibility tarball so the job can be rerun.
// Same registry reconciliation as publish.cjs, for one package: a 404 permits the publish,
// equal dist.integrity skips it, different integrity stops, and every publish response
// (including an error or timeout) is followed by bounded read-back of integrity, signed
// provenance and the latest tag. Run with: node publish-compat.cjs --tarball F --name N --version V
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const {resolveNpmCli,createVerifier,publishingIdentity}=require('./provenance.cjs');
const {readbackDelays}=require('./readback.cjs');
function option(name) {
  const index=process.argv.indexOf(name);
  assert.ok(index>=0 && process.argv[index+1],`${name} required`);
  return process.argv[index+1];
}
const tarball=path.resolve(option('--tarball')),name=option('--name'),version=option('--version');
// Tests shorten the wait with --readback-delays-ms '[0,1,1]'. Releases use the default window.
const delaysIndex=process.argv.indexOf('--readback-delays-ms');
const delays=delaysIndex<0?readbackDelays():JSON.parse(process.argv[delaysIndex+1]);
assert.ok(Array.isArray(delays) && delays.length>0 && delays.every(ms=>Number.isInteger(ms) && ms>=0),'--readback-delays-ms must be a JSON array of non-negative integers');
assert.match(version,/^\d+\.\d+\.\d+$/);
assert.ok(fs.lstatSync(tarball).isFile() && !fs.lstatSync(tarball).isSymbolicLink(),'tarball must be a regular file');
const bytes=fs.readFileSync(tarball);
const sha512=crypto.createHash('sha512').update(bytes).digest();
const integrity='sha512-'+sha512.toString('base64');
const npmCli=resolveNpmCli();
const tufCache=fs.mkdtempSync(path.join(os.tmpdir(),'brigade-compat-tuf-'));
const verifier=createVerifier(npmCli,tufCache);
function npm(args) {
  const result=spawnSync(process.execPath,[npmCli,...args],{encoding:'utf8',timeout:120000,maxBuffer:1024*1024,
    env:{...process.env,npm_config_registry:'https://registry.npmjs.org/',npm_config_fetch_retries:'0',npm_config_fetch_timeout:'30000'}});
  let json;
  try {json=JSON.parse(result.stdout);} catch {}
  return {...result,json};
}
const read=field=>npm(['view',`${name}@${version}`,field,'--json']);
function state() {
  const r=read('dist.integrity');
  if(r.status===0 && typeof r.json==='string') {
    if(r.json!==integrity) throw new Error(`Immutable registry integrity mismatch: ${name}@${version} is live with different bytes`);
    return 'equal';
  }
  if(r.status!==0 && r.json?.error?.code==='E404') return 'absent';
  if(r.status===0 && (r.stdout || '').trim()==='') {
    const lookup=npm(['view',name,'versions','--json']);
    const versions=Array.isArray(lookup.json)?lookup.json:typeof lookup.json==='string'?[lookup.json]:[];
    if(lookup.status===0 && versions.length && versions.every(v=>typeof v==='string' && /^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(v)) && !versions.includes(version)) return 'absent';
  }
  return 'unknown';
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
// Same policy as publish.cjs: repository name from the workflow, repository ID bound, nothing literal.
const expected=publishingIdentity(process.env,name,version,verifier);
async function reconcile(field) {
  for(const delay of delays) {
    if(delay) await pause(delay);
    if(field==='dist.integrity') {if(state()==='equal') return;}
    else {
      const r=read(field);
      if(r.status===0 && r.json?.url) {
        try {
          if(await verifier.verifyRegistry(r.json.url,sha512.toString('hex'),expected)) return;
        } catch(error) {throw new Error(`Invalid registry provenance: ${name}@${version}: ${error.message}`,{cause:error});}
      }
    }
  }
  throw new Error(`Registry reconciliation incomplete: ${name}@${version} ${field}. Rerun this job after inspection.`);
}
async function main() {
  const before=state();
  if(before==='unknown') throw new Error(`Cannot establish registry state before publish: ${name}@${version}`);
  if(before==='absent') {
    const r=npm(['publish',tarball,'--provenance','--access','public','--tag','latest','--ignore-scripts']);
    // Even a timeout or E403 may follow a successful registry write.
    console.log(JSON.stringify({name,publishExit:r.status,uncertain:!!r.error || r.status!==0}));
  } else console.log(JSON.stringify({name,version,status:'already published with identical bytes, publish skipped'}));
  await reconcile('dist.integrity');
  await reconcile('dist.attestations');
  for(const delay of delays) {
    if(delay) await pause(delay);
    const latest=npm(['view',name,'dist-tags.latest','--json']);
    if(latest.status===0 && latest.json===version) {console.log(JSON.stringify({name,version,integrity,status:'verified'}));return;}
  }
  throw new Error(`Registry reconciliation incomplete: ${name} dist-tags.latest did not reach ${version}. Rerun this job after inspection.`);
}
main().catch(error=>{console.error(error.message);process.exitCode=1;}).finally(()=>fs.rmSync(tufCache,{recursive:true,force:true}));
