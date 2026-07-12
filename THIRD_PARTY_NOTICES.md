# Third-party notices

Two kinds of entries live here. **License notices** carry the legal text that
must travel with derived code. **Acknowledgments** credit projects whose ideas
shaped Brigade with no code copied, and each links the write-up that explains
what we took and what we did differently. New entry every time a blog post nods to
another project.

## License notices

### alp-river

`src/brigade/router.py` is a derivative of `hooks/route.py` from
[alp-river](https://github.com/alp82/alp-river), used under the MIT license.

```
MIT License

Copyright (c) 2026 Alper Ortac

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The `latent-premises` and `retry-safety` registry skills adapt review
criteria (the premise taxonomy and the retry/migration standard) from
alp-river's correctness reviewer, credited in each skill's footer.

## Acknowledgments

Ideas, not code. Nothing in this section creates a license obligation. The
entries exist because credit should outlive the commit message.

### ActiveGraph (Yohei Nakajima)

Brigade's outcome ledger follows the ledger-projection pattern from
[ActiveGraph](https://github.com/yoheinakajima/activegraph) and Yohei
Nakajima's papers "The Log is the Agent"
([arXiv:2605.21997](https://arxiv.org/abs/2605.21997)) and "Regimes"
([arXiv:2606.10241](https://arxiv.org/abs/2606.10241)): an append-only record
log as the source of truth, state as a pure projection, and a read-only drift
check before anything trusts the fold. No ActiveGraph code is vendored or
depended on. Details: `docs/design/activegraph-inspiration.md` and the
write-up at <https://brigade.tools/blog/activegraph-loop>.

### alp-river (Alper Ortac)

Beyond the derived router above, alp-river's larger idea shaped the route
brief: routing decisions belong in tested code, with signals pulling stages
into a composed pipeline and while/until locks gating the irreversible ones.
Write-up: <https://brigade.tools/blog/porting-alp-rivers-router-into-brigade>.
