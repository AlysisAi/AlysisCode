#!/usr/bin/env bash
# Trusted-host setup only. No model/API credentials are accepted or persisted.
# Run explicitly as root on the new, tagged synthetic pilot control VM.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates docker.io python3-venv python3-pip
systemctl enable --now docker

id alysis >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin alysis
install -d -m 0755 /srv/alysis

# The template creates exactly one new empty data disk at LUN 0. Refuse to format
# any disk that already contains a filesystem or partition table. Repeated setup
# mounts the existing ext4 data; it never overwrites a previous database.
disk=/dev/disk/azure/scsi1/lun0
test -b "$disk"
filesystem=$(blkid -s TYPE -o value "$disk" || true)
if [ -z "$filesystem" ]; then
  test -z "$(blkid -p "$disk" || true)"
  mkfs.ext4 -q "$disk"
elif [ "$filesystem" != ext4 ]; then
  echo 'Unexpected pilot data filesystem; no changes made.' >&2
  exit 1
fi
install -d -m 0700 /srv/alysis-data
if ! mountpoint -q /srv/alysis-data; then
  mount -o nodev,nosuid "$disk" /srv/alysis-data
fi
chown root:alysis /srv/alysis-data
chmod 0750 /srv/alysis-data
uuid=$(blkid -s UUID -o value "$disk")
python3 - "$uuid" <<'PY'
import pathlib,sys
path=pathlib.Path('/etc/fstab')
text=path.read_text()
line=f'UUID={sys.argv[1]} /srv/alysis-data ext4 defaults,nodev,nosuid,nofail 0 2'
if '/srv/alysis-data' not in text:
    path.write_text(text.rstrip()+'\n'+line+'\n')
PY
install -d -m 0700 -o alysis -g alysis /srv/alysis-data/controller
install -d -m 0700 /srv/alysis-data/postgres
python3 -m venv /srv/alysis/venv
echo 'Host tools and persistent data disk ready.'
