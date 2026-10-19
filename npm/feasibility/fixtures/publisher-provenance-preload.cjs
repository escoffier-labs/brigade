'use strict';
// Orchestration only. Real crypto is tested separately against the public bundle.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const target=require.resolve('../provenance.cjs');
const real=require(target);
require.cache[target].exports={...real,createVerifier:(cli)=>{
  assert.equal(cli,fs.realpathSync(process.env.npm_execpath));
  const purl=(name,version)=>`pkg:npm/${name.replace(/^@/,'%40')}@${version}`;
  return {purl,verifyRegistry:async(url,sha512,expected)=>{
    const root=process.env.PUBLISH_FIXTURE,metadata=JSON.parse(fs.readFileSync(path.join(root,'artifacts.json')));
    const artifact=metadata.artifacts.find(a=>purl(a.name,metadata.runtime.version)===expected.purl);
    assert.ok(artifact);
    assert.equal(sha512,crypto.createHash('sha512').update(fs.readFileSync(path.join(root,artifact.filename))).digest('hex'));
    assert.deepEqual(expected,{purl:purl(artifact.name,'0.29.0'),sourceCommit:metadata.sourceCommit,repository:'https://github.com/escoffier-labs/brigade',workflow:'.github/workflows/npm-publish.yml',ref:'refs/heads/main',san:'https://github.com/escoffier-labs/brigade/.github/workflows/npm-publish.yml@refs/heads/main',issuer:'https://token.actions.githubusercontent.com',builder:'https://github.com/actions/runner/github-hosted'});
    assert.equal(url,'https://registry.npmjs.org/-/npm/v1/attestations/'+artifact.name+'@0.29.0');
    fs.appendFileSync(path.join(root,'calls.jsonl'),JSON.stringify(['verify',artifact.name,sha512,expected])+'\n');
    if(process.env.PUBLISH_STATE==='wrong-commit') throw new Error('certificate source commit mismatch');
    return {sourceCommit:expected.sourceCommit};
  }};
}};
