# Brigade npm distribution

The approved stable package set is `@escoffier-labs/brigade@0.30.0` and five exact-version uv companions. The launcher bundles the official published `brigade-cli==0.30.0` wheel, unchanged. Runtime installation uses that wheel without a PyPI fallback.

```sh
npx --yes @escoffier-labs/brigade@0.30.0 --version
npm install --global @escoffier-labs/brigade@0.30.0
brigade --version
```

The explicit package form also works: `npx --yes --package=@escoffier-labs/brigade@0.30.0 -- brigade --version`. Installing with `--omit=optional` prevents startup. Reinstall with optional dependencies enabled. Linux glibc x64/ARM64, macOS x64/ARM64 and Windows x64 are supported targets. Unsupported platforms should use the Python installation route. Global npm bins can overlap existing pipx or uv installations, so inspect command resolution after installation.

## Preparation and local evaluation

Preparation requires Python 3.10+, Node 24.22.0 and npm 11.19.1. It downloads the exact wheel URL and SHA-256 in `runtime.json`, with a 64 MiB download bound and hash verification before use. It validates metadata, WHEEL tags, MIT license, extra-only dependencies, archive paths and RECORD hashes.

Every Brigade package member must match the `v0.30.0` tag at `02270bc7ffd92640cd8debf39711eca7c8678fc0`. The sole exception is `brigade/templates/components/manifest-v1.json`: it must equal the pinned release asset and its checksum, with the exact release version and component revisions. A reference build from the tag with `build-requirements.txt` enforces reverse package coverage. That build checks the inventory and does not replace the published wheel. Hosted CI also verifies the manifest's GitHub attestation.

```sh
python3 npm/feasibility/prepare.py --out /tmp/brigade-npm-artifacts
```

Default preparation refuses tracked and nonignored untracked changes. Local iteration can use `--allow-dirty`, which records `sourceTree: dirty` and a file-hash packaging identity. The stable payload retains its separate tag identity. Such artifacts cannot establish a clean-commit build. Generated packages, reference build environments, caches and receipts stay outside the checkout. Hosted gates require a clean reviewed commit.

All six manifests use public access and repository metadata. Five companions contain only the matching uv 0.12.23 executable, manifest and uv MIT/Apache licenses. Preparation verifies official uv archive, executable and license hashes. The launcher contains the validated wheel, runtime manifest and Brigade MIT license. npm packing disables lifecycle scripts and retains SHA-256, npm SHA-512 integrity and shasum for every original archive.

For local packed evaluation, select the launcher tarball and your platform's companion from `artifacts.json`:

```sh
npm install --offline --ignore-scripts --prefix /tmp/brigade-evaluation /absolute/launcher.tgz /absolute/companion.tgz
npx --offline --yes --package /absolute/launcher.tgz --package /absolute/companion.tgz -- brigade --version
```

Those filenames are placeholders for the exact locally prepared tarballs.

## Publication and retry behavior

`npm-publish.yml` accepts only an explicit dispatch at the expected clean main SHA and release tag. It builds once with the pinned Node/npm toolchain, retains original archives for 14 days, and requires five native packed acceptance jobs before the npm environment's OIDC publish job. The publisher verifies original archive hashes, publishes and verifies each companion first, then publishes the launcher with mandatory provenance.

Before each publish, a registry 404 permits publication, equal integrity permits a skip, and different integrity stops the run. Unknown registry state stops before writing. Every publish response, including an error or timeout, requires bounded read-back reconciliation. The read-back waits up to about 10 minutes per check (15 second first wait, doubling to a 60 second cap) because npm can take several minutes to show a new version, and the final `dist-tags.latest` read uses the same window. The window applies to each check, and one 80 minute deadline bounds all waiting in the run. When a degraded registry reaches that deadline, the publisher stops with `Registry reconciliation incomplete` before the 105 minute step limit, and a rerun skips versions whose integrity already matches. For a retry, choose **Re-run failed jobs** in the same workflow run within the 14-day artifact retention. This reuses the successful build job's original archives. **Re-run all jobs** and a new dispatch rebuild archives and do not reuse the originals. Preserve the original archives and inspect any partial registry publication before rebuilding. An immutable-version mismatch requires a new coordinated version, never an overwrite. All six versions must expose provenance attestations. Five native registry acceptance jobs and the final release gate follow publication.

## Runtime and private state

Runtime users need Node 22 or newer, as declared in the launcher's `engines` field, and npm. The pinned Node 24.22.0 and npm 11.19.1 toolchain applies only to preparation and publication. Git is not bundled. First use downloads pinned managed CPython 3.12.15, build 20261003, through uv's catalogue. Brigade itself is installed solely from the bundled absolute wheel path using `uv --no-config tool install --managed-python --python 3.12.15 --no-index`. There is no package-index fallback. A corrupt wheel or companion fails before execution.

`BRIGADE_NPM_CACHE` selects the state root. The default is `~/.cache/brigade-npm`, including on Windows. `payloads/<full-wheel-sha256>/` holds that payload's tool environment, uv cache and initialization marker. uv command shims live inside their owning tool environment, so deleting it also removes stale shims before reinstall. `python/<version>-<build>/` shares only the exact managed Python identity between payloads. No automatic pruning occurs. Service units installed through npm pin that payload’s `sys.executable`. Reinstall those units after each npm upgrade and retain every payload cache still referenced by a unit. Stop all users of a state directory before deleting it manually.

The atomic initialized marker binds the full wheel SHA, uv version, Python version/build and the private identity file's path, bytes and hash. Warm startup validates the payload, interpreter, actual sysconfig purelib and identity file, then skips uv. Missing or mismatched state reprovisions or fails. Provisioning errors cannot create a success marker. uv owns concurrent installation locking.

The identity file is a minimal `.pth` inside only the private tool venv's actual site-packages. It marks Brigade as npm-managed for the launcher and that interpreter's normal or isolated `-m brigade` respawns. It is written atomically before the initialized marker. System, pipx, caller, development and shared managed-base environments are not modified. `brigade update` refuses npm-managed installations before state, network or pipx work. Install another exact version with npm install or npx. Passive pipx update checks are disabled by interpreter-local identity.

The application runs through the private absolute interpreter with `-I` and receives the caller's original environment. Verification children receive that same environment. The launcher adds neither `BRIGADE_DISTRIBUTION` nor `BRIGADE_NO_UPDATE_CHECK`. Existing caller values remain caller preferences. Python isolated mode prevents user-site and Python environment overrides from redirecting Brigade itself. Other interpreters invoked by user commands retain their own behavior.

During provisioning, inherited uv configuration and Python/virtualenv overrides are removed. `UV_OFFLINE`, `UV_PYTHON_DOWNLOADS`, `UV_NATIVE_TLS`, `UV_SYSTEM_CERTS`, `UV_HTTP_TIMEOUT`, `UV_HTTP_CONNECT_TIMEOUT` and `UV_HTTP_RETRIES` remain binding. Proxy and certificate settings pass through with certificate verification enabled. `BRIGADE_NPM_PYTHON_MIRROR` is the explicit Python mirror opt-in. Package-index opt-ins are unused.

A cold offline start without managed Python fails with download guidance. Once Python is cached, the bundled wheel supports offline tool-environment installation and reinstall. Ordinary startup does not initialize a workspace, install native engines or register schedulers.

The user-selected cache root may have a symlinked ancestor or alias. On POSIX, the selected root must belong to the current user and have no group or other write permission. Unsafe existing roots are refused without changing their permissions. Paths inside the launcher-owned subtree, including all three managed-Python directories, must remain real directories. Windows ACL ownership and write restrictions are not validated by these POSIX checks. Alias and canonical startups share the same initialized state. Cancellation during warm interpreter validation preserves the initialized marker and does not start another uv install.

## Acceptance and evidence limits

Run repository checks through Brigade's verification capture. `launcher.test.cjs` uses subprocess stand-ins for transport and lifecycle boundaries. `acceptance.cjs` installs the exact tarballs under a temporary HOME with a fresh npm cache and a PATH containing Node, Git and required OS utilities, with Python and uv absent. The approved disposable Docker route confines Linux process signals to its own PID namespace and removes the container on exit.

Acceptance exercises the actual cold npx CLI before any prefix installation. Packed mode uses local offline inputs. `--registry-spec @escoffier-labs/brigade@0.30.0` uses a fresh online npm cache, HOME and empty npmrc against the public registry. Only the launcher is requested, so npm resolves its companion through optional dependencies. Registry mode compares both installed lockfile integrities, including the npx cache installation, to the original archive receipt and runs `npm audit signatures`. It retains direct and npm exec checks, argv/nonzero behavior, concurrency, initialization recovery, optional-companion omission, integrity rejection, offline restrictions and cached-Python reinstall. Installed `__init__.py` and `verify_runtime.py` must match the bundled wheel and RECORD. Private-interpreter respawns exercise update refusal and passive gates under seeded cache state and deterministic side-effect sentinels. Receipts include actual artifact hashes, source identity, runtime versions, timings and disk usage. Managed Python network transfer bytes are not metered.

Catchable POSIX INT, TERM and HUP tests target both the launcher PID and process group while a real verification command owns a detached grandchild. Acceptance fails nonzero on surviving test descendants or missing/invalid canonical canceled receipts. Cleanup must finish before the launcher's 20-second forced termination. Repeated application cancellation forwards the original signal without resetting that deadline. Provisioning repeats can force termination.

Linux and Darwin descendant tracking retains a low residual risk from numeric PID reuse. Detached or reparented orphans outside the tracked identities can escape cleanup. Darwin timeout cleanup still requires native qualification separate from catchable cancellation.

SIGKILL, OOM termination and the launcher's forced deadline cannot run user-space cleanup. Detached verification processes can survive those cases. Default native acceptance runs only the six catchable POSIX cases. The deliberate SIGKILL case requires `--contained-linux-uncatchable` in a disposable Linux PID namespace, an externally observed distinct host namespace and bounded external teardown. That contained case records and cleans only its pinned test identities. No OOM test is run. Windows external `taskkill /T /F` proves forced tree termination separately from console Ctrl+C, which remains unproved. Killing only the Windows launcher cannot guarantee cleanup without a job object.

Native macOS x64 acceptance of the published 0.29.0 packages ended the HUP case with a SIGHUP exit instead of the expected status 130, after the canonical canceled receipt and descendant cleanup had completed. The same result appeared in native qualification of an unpublished candidate. The order of events that produced the SIGHUP exit in the original run is unproved. macOS ARM64 passed the full native run, and Linux and Windows registry acceptance passed. The 0.29.0 launcher runs `brigade.cli:main` from the unchanged 0.29.0 wheel, so changes to CLI shutdown handling after 0.29.0 reach npm users only through a new coordinated package version. The 0.30.0 launcher runs `brigade.cli:process_main`, the same entry point as the `brigade` console script, so npm users get the CLI shutdown handling from #1806.

Hashes stored beside payloads detect corruption and mixed installs. They do not authenticate a replaced tarball whose manifests were also replaced. `artifacts.json` records build provenance and exact transferred archive hashes. Registry acceptance verifies immutable archive integrity and publication provenance separately. Public CI artifacts are downloadable by signed-in GitHub users during their retention period. Local Linux acceptance does not establish native macOS, Windows or ARM behavior. Final clean-source builds, CI and independent reviews remain separate gates.
