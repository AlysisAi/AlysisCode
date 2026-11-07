# Sandbox vulnerability applicability

The comprehensive OS and library scan remains blocking for unassessed HIGH and
CRITICAL findings, including findings without a published fix. Original JSON,
CycloneDX, OpenVEX and final SARIF reports are retained for each scanned image.

## pip's setuptools metadata — 7 November 2026

The signed Wolfi `py3.12-pip-base=26.2.2-r2` package and its ensurepip seed wheel
list setuptools 70.3.0 in their embedded metadata. They contain only its
`pkg_resources` subset. They do not contain `setuptools.package_index`, whose
`PackageIndex.download` implementation is affected by
[CVE-2025-47273](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf).
The complete system setuptools package is independently pinned to 84.0.0.

This assessment covers only that SBOM-derived entry from the reviewed pip package
layer. It does not cover an application-installed copy of setuptools 70.3.0.
The complete installed pip tree, including bytecode and symlink targets, and the
seed wheel are fingerprinted in `vulnerability-assessments.json`. Original vendor
metadata remains intact. A changed provider version, changed file, unexpected
analyzer, physical package copy or additional installation layer fails assessment.

`assess_vulnerabilities.py` binds the reviewed statement to the exact scanned
image and package PURLs after verifying image identities, package provenance and
file fingerprints. New CVEs receive no statement. There is no global CVE ignore.

When pip changes, review both installed and seeded contents against the upstream
advisory before changing fingerprints. Run the assessment regression tests and
the complete scan and smoke checks on both architectures. Remove the statement
when the metadata no longer reports the absent vulnerable component.
