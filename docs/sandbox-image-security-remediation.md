# Sandbox image security remediation — 29 September 2026

Status: **rebuilt image still fails its blocking security scan; release held**.
These are the sandbox dependency updates included with the IDE source release.
They do not establish that the rebuilt images pass their security gates.

## Baseline evidence

The existing [sandbox scan run 36262283540](https://github.com/AlysisAi/AlysisCode/actions/runs/36262283540),
attempt 1, used source `2c2002231a533fa7c9265d33274f018410c714c1`. Its retained
`trivy-pr-dev-amd64-sarif` artifact has 536 findings: 522 HIGH, 14 CRITICAL,
337 distinct CVE IDs, and 169 package findings without a fixed version in that report.
Those counts describe the old image. They are not a scan of the current Dockerfile.

## Prepared changes

| Component | Old input | Prepared input and reason |
| --- | --- | --- |
| Python base | Older `python:3.12-slim` digest | Official `3.12.14-slim-trixie` multi-platform digest, checked for amd64 and arm64. Explicit distro prevents a moving distro alias. |
| Debian | Snapshot `20260701T000000Z`; only requested tools installed | Snapshot `20260927T000000Z`; upgrade inherited libraries before installing tools. Both Debian and Debian Security snapshot Release URLs returned HTTP 200. APT must still validate their signed metadata during a real build. |
| Go | 1.26.4 | 1.26.8, staying on the same minor line; both platform hashes checked against [Go's download metadata](https://go.dev/dl/?mode=json). |
| Node | 24.18.1 | 24.21.0 LTS; both downloaded Linux archives matched [official SHA-256 values](https://nodejs.org/dist/v24.21.0/SHASUMS256.txt). |
| npm | Node's bundled distribution | Independent 11.20.0 tarball with SHA-256 pin, also verified against the [registry's SHA-512 integrity](https://registry.npmjs.org/npm/11.20.0). This avoids a major npm upgrade and is compatible with the selected Node version. |

Node 24.21.0 alone was insufficient: archive inspection found npm 11.19.0 with
`brace-expansion` 5.0.7, `ip-address` 10.2.0, and `tar` 7.5.19. The complete npm
11.20.0 distribution contains `brace-expansion` 5.0.9, `ip-address` 10.5.0,
`pacote` 21.5.1, `tar` 7.5.22, and `undici` 6.28.0. These meet the fixed-version
requirements for those packages in the retained scan. The Dockerfile verifies the
tarball before replacement and verifies both npm/npx versions afterwards.

No CVE was added to `.github/.trivyignore`; severity thresholds, `ignore-unfixed: false`,
and blocking exit codes remain intact. Do not treat kernel-header findings or
packages with no recorded fix as automatically exempt.

## Public candidate scan

[Public candidate run 36561233104](https://github.com/AlysisAi/AlysisCode/actions/runs/36561233104)
built the amd64 dev image from candidate commit
`3d99fd430ce95f1b9fb6c338ae437754719570de`. Its retained
`trivy-pr-dev-amd64-sarif` artifact reports 168 HIGH findings, no CRITICAL findings,
and 109 distinct CVEs. All remaining findings have no fixed version recorded in
that Debian trixie scan. The image build succeeded; the security gate failed.
These counts apply to that candidate, not subsequent source changes.

The follow-up removes the redundant Debian `python3`/`python3-venv` installation.
The official Python base already provides the interpreter, pip, and venv; a new
build-time check creates a virtual environment and verifies its pip. This avoids
shipping an unused second interpreter and bundled setuptools wheel. Its impact
was checked in [follow-up run 36562492600](https://github.com/AlysisAi/AlysisCode/actions/runs/36562492600)
on source `1dec6c2dd8e8e8478ff6ac5178a888d9cc4cba74`. The dev amd64 image built,
but its scan reported 151 HIGH and 1 CRITICAL package findings, covering 105
distinct CVEs. The scan recorded no fixed versions, and runtime smoke was skipped
after the scan failed. The change did not close the security gate.

The critical finding is [CVE-2026-43185](https://security-tracker.debian.org/tracker/CVE-2026-43185),
reported against `linux-libc-dev` 6.12.107-1. Debian describes a kernel ksmbd buffer
overflow. This needs package-content and vulnerable-code applicability review:
the header-package finding alone neither proves the vulnerable kernel code ships
in the image nor establishes an exemption. The host kernel must be assessed
separately. No finding has been suppressed.

Vendor review identifies real blockers, not merely stale package indexes:

- [libacl CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369):
  trixie 2.3.2 remains vulnerable; fixed 2.4.0 is in testing/unstable. Debian notes
  that the ABI change is not suitable for an individual patch backport.
- [Expat CVE-2026-66046](https://security-tracker.debian.org/tracker/CVE-2026-66046)
  and [CVE-2026-76956](https://security-tracker.debian.org/tracker/CVE-2026-76956):
  installed 2.8.3 remains vulnerable; the vendor records fixes from 2.8.4.
- [util-linux CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642):
  installed 2.41.5 remains vulnerable; Debian's fixed packages are in
  testing/unstable, outside this stable image's package sources.
- [Perl CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538):
  the stable fix is postponed while upstream regressions are resolved.

Debian's minor-issue/no-DSA classification does not override this repository's
blocking HIGH/CRITICAL policy. Neither switching severity feeds nor mixing
unstable binary packages into the stable base establishes remediation. The 89
kernel-header findings and source-package findings on libraries still require
individual applicability evidence; none has been exempted.

A September 29 check of an Ubuntu LTS alternative found that it is not a complete
drop-in remedy: [Ubuntu's ACL advisory](https://ubuntu.com/security/CVE-2026-54369)
also declines the intrusive backport, although its
[Expat advisory](https://ubuntu.com/security/CVE-2026-66046) records fixed LTS
packages. Changing the base requires a package-by-package review and fresh builds;
a different vendor severity classification is not evidence that a defect is fixed.

## Verification still required

On an available build host, build `base`, `dev`, and `server` for both amd64 and arm64
from the reviewed source. Retain the exact image digests, current Trivy database
identity, six blocking scan results, package inventories, and runtime smoke output.
Recheck every remaining CVE against the distro/vendor advisory and exact installed
version. Apply an available patch or a reviewed dependency replacement/removal;
unresolved HIGH/CRITICAL findings continue to block. A source-input check or a
package-specific archive inspection cannot replace these scans.

The public sandbox workflow
must pass its own scans, smoke, signatures, and attestations. The VS Code managed
runtime has a separate locked-dependency audit and six-target signing gate; neither
gate substitutes for the other.
