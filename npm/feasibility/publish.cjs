'use strict';
// Publish the original archives only. Never repack or infer absence from a failed write.
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const {resolveNpmCli,createVerifier}=require('./provenance.cjs');
const index=process.argv.indexOf('--artifacts');
assert.ok(index>=0 && process.argv[index+1],'--artifacts required');
const root=path.resolve(process.argv[index+1]);
const metadata=JSON.parse(fs.readFileSync(path.join(root,'artifacts.json')));
assert.match(process.env.GITHUB_SHA || '',/^[a-f0-9]{40}$/,'GITHUB_SHA must identify the packaging commit');
assert.equal(metadata.sourceCommit,process.env.GITHUB_SHA,'artifact packaging commit must equal GITHUB_SHA');
const npmCli=resolveNpmCli();
const tufCache=fs.mkdtempSync(path.join(os.tmpdir(),'brigade-publish-tuf-'));
const verifier=createVerifier(npmCli,tufCache);
const version=metadata.runtime.version;
assert.equal(version,'0.30.0');assert.equal(metadata.runtime.payloadVersion,version);
const artifacts=metadata.artifacts;
assert.equal(artifacts.length,6);assert.equal(new Set(artifacts.map(a=>a.name)).size,6);
const companions=artifacts.filter(a=>a.platform),launchers=artifacts.filter(a=>!a.platform);
assert.equal(companions.length,5);assert.equal(launchers.length,1);assert.equal(launchers[0].name,'@escoffier-labs/brigade');
const filenames=new Set();
const archiveDigests=new Map();
for(const a of artifacts) {
  assert.ok(a.name.startsWith('@escoffier-labs/brigade'));
  assert.ok(a.filename===path.basename(a.filename) && !/[\\/]/.test(a.filename) && a.filename.endsWith('.tgz'));
  assert.ok(!filenames.has(a.filename));filenames.add(a.filename);
  const file=path.join(root,a.filename);assert.ok(fs.lstatSync(file).isFile() && !fs.lstatSync(file).isSymbolicLink());
  const bytes=fs.readFileSync(file);assert.equal(bytes.length,a.bytes);
  assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),a.sha256);
  assert.equal(crypto.createHash('sha1').update(bytes).digest('hex'),a.shasum);
  assert.equal('sha512-'+crypto.createHash('sha512').update(bytes).digest('base64'),a.integrity);
  archiveDigests.set(a.name,crypto.createHash('sha512').update(bytes).digest('hex'));
}
function npm(args) {
  const result=spawnSync(process.execPath,[npmCli,...args],{encoding:'utf8',timeout:120000,maxBuffer:1024*1024,
    env:{...process.env,npm_config_registry:'https://registry.npmjs.org/',npm_config_fetch_retries:'0',npm_config_fetch_timeout:'30000'}});
  let json;
  try {json=JSON.parse(result.stdout);} catch {}
  return {...result,json};
}
function read(a,field) {return npm(['view',`${a.name}@${version}`,field,'--json']);}
function integrity(a) {
  const r=read(a,'dist.integrity');
  if(r.status===0 && typeof r.json==='string') {
    if(r.json!==a.integrity) throw new Error(`Immutable registry integrity mismatch: ${a.name}@${version}`);
    return 'equal';
  }
  if(r.status!==0 && r.json?.error?.code==='E404') return 'absent';
  if(r.status===0 && (r.stdout || '').trim()==='') {
    const lookup=npm(['view',a.name,'versions','--json']);
    const versions=Array.isArray(lookup.json)?lookup.json:typeof lookup.json==='string'?[lookup.json]:[];
    const semver=/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][\da-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][\da-zA-Z-]*))*)?(?:\+[\da-zA-Z-]+(?:\.[\da-zA-Z-]+)*)?$/;
    if(lookup.status===0 && versions.length && versions.every(v=>typeof v==='string' && semver.test(v)) && !versions.includes(version)) return 'absent';
  }
  return 'unknown';
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function reconcile(a,field) {
  for(const delay of [0,1000,2000,4000,8000]) {
    if(delay) await pause(delay);
    if(field==='dist.integrity') {if(integrity(a)==='equal') return;}
    else {
      const r=read(a,field);
      if(r.status===0 && r.json?.url) {
        try {
          if(await verifier.verifyRegistry(r.json.url,archiveDigests.get(a.name),{
            purl:verifier.purl(a.name,version),sourceCommit:metadata.sourceCommit,
            repository:'https://github.com/escoffier-labs/brigade',workflow:'.github/workflows/npm-publish.yml',ref:'refs/heads/main',
            san:'https://github.com/escoffier-labs/brigade/.github/workflows/npm-publish.yml@refs/heads/main',
            issuer:'https://token.actions.githubusercontent.com',builder:'https://github.com/actions/runner/github-hosted',
          })) return;
        } catch(error) {throw new Error(`Invalid registry provenance: ${a.name}: ${error.message}`,{cause:error});}
      }
    }
  }
  throw new Error(`Registry reconciliation incomplete: ${a.name}@${version} ${field}. Retain original archives and retry after inspection.`);
}
async function main() {
  for(const a of [...companions,...launchers]) {
    const state=integrity(a);
    if(state==='unknown') throw new Error(`Cannot establish registry state before publish: ${a.name}`);
    if(state==='absent') {
      const r=npm(['publish',path.join(root,a.filename),'--provenance','--access','public','--tag','latest','--ignore-scripts']);
      // Even a timeout or E403 may follow a successful registry write.
      console.log(JSON.stringify({name:a.name,publishExit:r.status,uncertain:!!r.error || r.status!==0}));
    }
    await reconcile(a,'dist.integrity');
    await reconcile(a,'dist.attestations');
    console.log(JSON.stringify({name:a.name,version,integrity:a.integrity,status:'verified'}));
  }
  const latest=read(launchers[0],'dist-tags.latest');
  assert.equal(latest.status,0);assert.equal(latest.json,version,'latest must identify the stable launcher');
}
main().catch(error=>{console.error(error.message);process.exitCode=1;}).finally(()=>fs.rmSync(tufCache,{recursive:true,force:true}));
