'use strict';
// Release tooling only: use the verifier bundled with the exact publishing npm.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const SLSA='https://slsa.dev/provenance/v1';
const SOURCE_DIGEST='1.3.6.1.4.1.57264.1.13';
function resolveNpmCli() {
  if(process.env.npm_execpath) return fs.realpathSync(process.env.npm_execpath);
  // Publishing and crypto gates run on Linux; setup-node exposes a CLI symlink.
  for(const dir of (process.env.PATH || '').split(path.delimiter)) {
    const candidate=path.join(dir,'npm');
    try {if(!fs.statSync(candidate).isFile()) continue;fs.accessSync(candidate,fs.constants.X_OK);return fs.realpathSync(candidate);} catch {}
  }
  throw new Error('Cannot resolve the publishing npm CLI from PATH.');
}
function createVerifier(npmCli,cachePath) {
  const cli=fs.realpathSync(npmCli),req=createRequire(cli);
  const npmRoot=path.resolve(path.dirname(cli),'..');
  assert.equal(JSON.parse(fs.readFileSync(path.join(npmRoot,'package.json'))).version,'11.19.1','pinned npm required');
  assert.equal(req('sigstore/package.json').version,'4.1.1','pinned bundled sigstore required');
  const sigRequire=createRequire(req.resolve('sigstore'));
  assert.equal(sigRequire('@sigstore/verify/package.json').version,'3.1.1','pinned bundled verifier required');
  const sigstore=req('sigstore'),npa=req('npm-package-arg');
  async function verifyResponse(response,sha512,expected) {
    assert.match(expected.sourceCommit,/^[a-f0-9]{40}$/);
    assert.match(sha512,/^[a-f0-9]{128}$/);
    const entries=response?.attestations?.filter(a=>a.predicateType===SLSA);
    assert.ok(entries?.length===1,'exactly one SLSA v1 attestation required');
    const bundle=entries[0].bundle,vm=bundle?.verificationMaterial,envelope=bundle?.dsseEnvelope;
    const certificates=vm?.x509CertificateChain?.certificates || (vm?.certificate?[vm.certificate]:[]);
    assert.ok(envelope?.signatures?.length===1 && certificates.length===1 && certificates[0]?.rawBytes && !vm.publicKey,'keyless bundle with one signature and certificate required');
    assert.equal(envelope.payloadType,'application/vnd.in-toto+json');
    const der=Buffer.concat([Buffer.from([0x0c,40]),Buffer.from(expected.sourceCommit,'ascii')]);
    const escaped=expected.san.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    let signer;
    try {
      signer=await sigstore.verify(bundle,{certificateIssuer:expected.issuer,certificateIdentityURI:'^'+escaped+'$',certificateOIDs:{[SOURCE_DIGEST]:der},tufCachePath:cachePath,timeout:15000,retry:0});
    } catch(error) {throw new Error('Provenance cryptographic verification failed.',{cause:error});}
    assert.equal(signer.identity.subjectAlternativeName,expected.san,'exact certificate SAN');
    assert.equal(signer.identity.extensions.issuer,expected.issuer,'exact certificate issuer');
    const oids=signer.identity.oids.filter(o=>o.oid.id.join('.')===SOURCE_DIGEST);
    assert.equal(oids.length,1,'certificate source digest OID');assert.deepEqual(oids[0].value,der,'certificate source commit');
    // Parse only the DSSE payload whose signature and identity just verified.
    const statement=JSON.parse(Buffer.from(envelope.payload,'base64').toString('utf8'));
    assert.equal(statement._type,'https://in-toto.io/Statement/v1');
    assert.equal(statement.predicateType,SLSA);
    assert.deepEqual(statement.subject,[{name:expected.purl,digest:{sha512}}],'signed archive subject');
    const definition=statement.predicate.buildDefinition;
    assert.equal(definition.buildType,'https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1');
    assert.deepEqual(definition.externalParameters.workflow,{ref:expected.ref,repository:expected.repository,path:expected.workflow},'signed workflow predicate');
    assert.deepEqual(definition.resolvedDependencies,[{uri:`git+${expected.repository}@${expected.ref}`,digest:{gitCommit:expected.sourceCommit}}],'signed source dependency');
    assert.equal(statement.predicate.runDetails.builder.id,expected.builder,'signed builder');
    return {sourceCommit:expected.sourceCommit,purl:expected.purl,sha512,san:signer.identity.subjectAlternativeName};
  }
  async function verifyRegistry(url,sha512,expected,fetcher=globalThis.fetch) {
    const target=new URL(url);
    assert.ok(target.origin==='https://registry.npmjs.org' && target.pathname.startsWith('/-/npm/v1/attestations/'),'invalid npm attestation URL');
    let response;
    try {response=await fetcher(target.href,{redirect:'error',signal:AbortSignal.timeout(30000)});} catch {return null;}
    if(response.status!==200) return null;
    let body;
    try {body=await response.json();} catch(error) {if(error instanceof SyntaxError) throw error;return null;}
    return verifyResponse(body,sha512,expected);
  }
  return {purl:(name,version)=>npa.toPurl(`${name}@${version}`),verifyRegistry};
}
module.exports={createVerifier,resolveNpmCli};
