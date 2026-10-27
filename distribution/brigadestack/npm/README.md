# Brigade Stack

`brigadestack@0.30.0` installs stable `@escoffier-labs/brigade@0.30.0` and adds
the `brigadestack` command. Existing packages and the `brigade` command remain
supported. This package exposes only `brigadestack`, so installing it globally
leaves an existing global `brigade` executable in place.

Requires Node.js 22.15 or newer in the 22.x line, or Node.js 23.11 or newer.
Keep optional dependencies enabled: the stable
dependency needs its platform runtime package. Its first launch provisions managed
Python and may need network access. Supported runtimes are Linux with glibc
(x64 or arm64), macOS (x64 or arm64), and Windows x64.

```sh
npm install --global brigadestack@0.30.0
brigadestack --version
brigadestack --help
```

For a project-local install:

```sh
npm install brigadestack@0.30.0
npm exec -- brigadestack --help
```

For a one-off invocation:

```sh
npm exec --yes --package=brigadestack@0.30.0 -- brigadestack --version
```

The launcher resolves its installed dependency independently of your working
directory and `PATH`, then runs it with the current Node executable. Arguments,
standard streams, environment, working directory, and CLI exit status pass through.
On POSIX, `process.execve` replaces the bridge with the dependency, preserving
its PID, process group, controlling terminal, and shell job control. The dependency
receives signals directly and owns runtime cleanup. On Windows, an attached child
inherits the console and receives console events directly.

The CLI reports Brigade 0.30.0. Commands, Python imports, workspace files, and
storage formats keep their existing identities.

[Homepage](https://brigade.tools) |
[Documentation](https://brigade.tools/docs) |
[Source](https://github.com/escoffier-labs/brigade/tree/main/distribution/brigadestack/npm) |
[Issues](https://github.com/escoffier-labs/brigade/issues)
