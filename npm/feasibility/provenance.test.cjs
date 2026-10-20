'use strict';
const {test,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {createVerifier,resolveNpmCli}=require('./provenance.cjs');
// Public registry evidence: https://registry.npmjs.org/-/npm/v1/attestations/sigstore@3.1.0
const response=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/sigstore-3.1.0-attestations.json')));
const cache=fs.mkdtempSync(path.join(os.tmpdir(),'brigade-provenance-tuf-'));
after(()=>fs.rmSync(cache,{recursive:true,force:true}));
const verifier=createVerifier(resolveNpmCli(),cache);
const url='https://registry.npmjs.org/-/npm/v1/attestations/sigstore@3.1.0';
// Supply recorded public registry bytes at the same boundary as production.
const verifyResponse=(body,digest,policy)=>verifier.verifyRegistry(url,digest,policy,async()=>({status:200,json:async()=>body}));
const expected={purl:'pkg:npm/sigstore@3.1.0',sourceCommit:'06cd267cbdc2eeaa04294995622db9f6ad2fa7cf',repository:'https://github.com/sigstore/sigstore-js',workflow:'.github/workflows/release.yml',ref:'refs/heads/main',san:'https://github.com/sigstore/sigstore-js/.github/workflows/release.yml@refs/heads/main',issuer:'https://token.actions.githubusercontent.com',builder:'https://github.com/actions/runner/github-hosted'};
const sha512='669cd60051c816ac85139e9d5ea817fc3903459773fab45c8e8224fd1414e085f4c220afd65f12ed9ad70c770273eb9a7feea8ef0de1da5de0e86606e5328df5';
const clone=()=>JSON.parse(JSON.stringify(response));
function bundle(r) {return r.attestations.find(a=>a.predicateType==='https://slsa.dev/provenance/v1').bundle;}
test('real public npm bundle verifies with exact foreign source policy',async()=>{
  assert.equal(verifier.purl('sigstore','3.1.0'),expected.purl);
  assert.equal(verifier.purl('@escoffier-labs/brigade','0.30.0'),'pkg:npm/%40escoffier-labs/brigade@0.30.0');
  const result=await verifyResponse(response,sha512,expected);
  assert.equal(result.sourceCommit,expected.sourceCommit);
});
for(const [name,change] of [
  ['SAN suffix beyond expected prefix',e=>{e.san=e.san.slice(0,-4);}],
  ['SAN regex wildcard',e=>{e.san=e.san.replace('sigstore-js','sigstore.js');}],
  ['Brigade workflow identity',e=>{e.san='https://github.com/escoffier-labs/brigade/.github/workflows/npm-publish.yml@refs/heads/main';}],
  ['issuer',e=>{e.issuer='https://invalid.example';}],
  ['certificate source commit',e=>{e.sourceCommit='0'.repeat(40);}],
  ['workflow predicate',e=>{e.workflow='.github/workflows/other.yml';}],
  ['source URI predicate',e=>{e.repository='https://github.com/example/other';}],
  ['builder predicate',e=>{e.builder='https://github.com/actions/runner/self-hosted';}],
  ['purl',e=>{e.purl='pkg:npm/other@3.1.0';}],
]) test('signed bundle rejects wrong '+name,async()=>{
  const e={...expected};change(e);
  await assert.rejects(verifyResponse(response,sha512,e));
});
test('signed subject rejects a different local archive digest',async()=>{
  await assert.rejects(verifyResponse(response,'0'.repeat(128),expected),/subject/);
});
test('duplicate SLSA entries are terminal',async()=>{
  const r=clone();r.attestations.push(r.attestations.find(a=>a.predicateType==='https://slsa.dev/provenance/v1'));
  await assert.rejects(verifyResponse(r,sha512,expected),/one SLSA/);
});
for(const field of ['gitCommit','subject','predicateType']) test('modified signed payload rejects cryptographically: '+field,async()=>{
  const r=clone(),b=bundle(r),s=JSON.parse(Buffer.from(b.dsseEnvelope.payload,'base64'));
  if(field==='gitCommit') s.predicate.buildDefinition.resolvedDependencies[0].digest.gitCommit='0'.repeat(40);
  else if(field==='subject') s.subject.push(s.subject[0]);
  else s.predicateType='https://invalid.example';
  b.dsseEnvelope.payload=Buffer.from(JSON.stringify(s)).toString('base64');
  await assert.rejects(verifyResponse(r,sha512,expected),/cryptographic/);
});
test('registry fetch is confined, bounded, non-redirecting and rejects unsigned evidence',async()=>{
  let called=0;
  const fetcher=async(url,options)=>{called++;assert.equal(options.redirect,'error');assert.ok(options.signal);return {status:200,json:async()=>({attestations:[{predicateType:'https://slsa.dev/provenance/v1',bundle:{dsseEnvelope:{signatures:[]},verificationMaterial:{}}}]})};};
  await assert.rejects(verifier.verifyRegistry('https://registry.npmjs.org/-/npm/v1/attestations/example@1.0.0',sha512,expected,fetcher),/keyless/);
  assert.equal(called,1);
  for(const url of ['http://registry.npmjs.org/-/npm/v1/attestations/x','https://example.com/-/npm/v1/attestations/x','https://registry.npmjs.org/other']) await assert.rejects(verifier.verifyRegistry(url,sha512,expected,fetcher),/attestation URL/);
  assert.equal(called,1);
  assert.equal(await verifier.verifyRegistry('https://registry.npmjs.org/-/npm/v1/attestations/x',sha512,expected,async()=>({status:404})),null);
  assert.equal(await verifier.verifyRegistry('https://registry.npmjs.org/-/npm/v1/attestations/x',sha512,expected,async()=>{throw new Error('unavailable');}),null);
});
