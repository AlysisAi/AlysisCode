# Reviewed picomatch security rebuild

`picomatch-4.0.8-alysis.1.tgz` is a local rebuild, not an official upstream/npm
release. It retains the MIT license and all runtime files from npm picomatch 4.0.7,
with only the parser fix proposed in [upstream PR 254](https://github.com/micromatch/picomatch/pull/254),
commit `c63a5bf4e61dbdb81bc41e97dc2616c30acce337`, and explicit rebuild metadata.
The fix flattens a single wildcard repeated extglob instead of nesting unbounded
regex repetition (GHSA-fc5q-39xc-vjpf). Matching and the outer capture are preserved;
an inner wildcard capture describes the flattened body rather than the last repeat.

Archive SHA-256: `8c61f1661413ac40be9e4cdff256dd836f8af1fe31aa66c3822d17d46262d0c8`.
The embedded provenance records the original registry integrity and both parser hashes.
Reproduce it from the repository root:

```sh
python scripts/security/rebuild_picomatch.py --output scripts/sandbox/npm-rebuild/vendor/picomatch-4.0.8-alysis.1.tgz
```

The builder verifies the exact archive digest, frozen lock integrity, installed version,
parser fingerprint, matching regressions and hostile-input process deadlines. npm audit
and image vulnerability scanning remain enabled without advisory exceptions. Replace
this rebuild with an official fixed release after equivalent validation.
