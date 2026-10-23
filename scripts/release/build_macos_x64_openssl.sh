#!/usr/bin/env bash
# Use static OpenSSL when cryptography has to compile from source on Intel macOS.
# This avoids colliding with Python's different libssl.3.dylib in a one-file bundle.
set -euo pipefail
test "${RUNNER_OS:-}" = macOS
test "$(uname -m)" = x86_64
test -n "${RUNNER_TEMP:-}"
openssl_version=3.5.9
openssl_sha256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
archive="$RUNNER_TEMP/openssl-$openssl_version.tar.gz"
prefix="$RUNNER_TEMP/alysis-openssl-x64"
curl --fail --location --retry 3 \
  "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz" \
  --output "$archive"
test "$(shasum -a 256 "$archive" | awk '{print $1}')" = "$openssl_sha256"
tar -xzf "$archive" -C "$RUNNER_TEMP"
cd "$RUNNER_TEMP/openssl-$openssl_version"
./Configure darwin64-x86_64-cc no-shared no-module -fPIC "--prefix=$prefix" --libdir=lib
make -j3
make HARNESS_JOBS=3 test
make install_sw
test -f "$prefix/lib/libssl.a"
test -f "$prefix/lib/libcrypto.a"
