# Frozen sandbox APK inputs

These locks freeze all 99 installed OS packages for each architecture, including
transitive dependencies and packages inherited from the digest-pinned base.

`packages.lock` binds package names to exact versions. `SHA256SUMS` binds each
archive's complete bytes. `manifest.json` records the observed installed APK
checksums, source inventory hash, repository URLs, sizes, and archive hashes.
The source repository is the one configured by the pinned Chainguard base.

The Dockerfile fetches only those versions and checks every archive hash.
The repository uses a signed APKINDEX to authenticate package checksums; these
archives do not carry individual signatures. The builder retains and verifies
that original signed index. The offline installation verifies it again and lets
native APK validate the packages against it, with no untrusted-package option.
It compares the complete resulting installed graph to the lock. A
missing archive, invalid signature, changed bytes, or graph difference fails
the build. There is no fallback to a newer package or untrusted APK.

The base CI jobs retain the complete archive sets, signed index and its hash,
lock manifests, public verification keys, and repository configuration as build
inputs. Runtime image layers do not contain these archived inputs. Retain release
inputs for the supported release lifetime and revalidate signatures, scans, and
attestations when updating them.

To prepare a reviewed replacement graph from a new trial receipt:

```sh
python scripts/sandbox/lock_apk_inputs.py \
  --inventory /path/to/wolfi-packages.txt --arch amd64 \
  --output-dir scripts/sandbox/apk-locks/amd64 \
  --cache-dir /path/outside/runtime/apk-inputs/amd64
```

Repeat for arm64. The generator does not authenticate packages by itself;
native APK verifies the signed index and package checksums in the Docker build. Review
the package delta and rerun all six image checks after changing either lock.
