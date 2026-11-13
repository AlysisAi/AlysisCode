"""Build the credential-free, Docker-free private macOS Bot computer image.

Supply an internal engine wheel and uv-lock-exported hashed requirements with the
``documents`` extra (``image_requirements.py``).
Only the image-building VM has a read-only package share. Task VMs have none.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import image_requirements  # noqa: E402
import license_pack  # noqa: E402


def digest(path, algorithm="sha256"):
    with path.open("rb") as source:
        return hashlib.file_digest(source, algorithm).hexdigest()


def download(url, destination, expected, algorithm):
    if destination.is_file() and digest(destination, algorithm) == expected:
        return
    temporary = destination.with_suffix(".download")
    with urllib.request.urlopen(url, timeout=60) as source, temporary.open("wb") as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    if digest(temporary, algorithm) != expected:
        temporary.unlink()
        raise ValueError("Downloaded image component failed verification.")
    temporary.replace(destination)


def read_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        part = connection.recv(min(65536, count - len(result)))
        if not part:
            raise ValueError("Image builder connection closed.")
        result.extend(part)
    return result


def rpc(path, value, timeout=5):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        body = json.dumps(value).encode()
        connection.sendall(len(body).to_bytes(4, "big") + body)
        size = int.from_bytes(read_exact(connection, 4), "big")
        if not 0 < size <= 8 * 1024 * 1024:
            raise ValueError("Invalid image builder response.")
        response = json.loads(read_exact(connection, size))
        if response.get("ok") is not True:
            raise ValueError("Image builder operation failed.")
        return response["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--requirements", required=True, type=Path)
    args = parser.parse_args()
    missing = image_requirements.missing_document_packages(args.requirements)
    if missing:
        parser.error(
            "The requirements lack the engine's document toolkit ("
            + ", ".join(missing)
            + "). Export them with: "
            + image_requirements.EXPORT
        )
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("This qualified image recipe requires an Apple Silicon Mac.")
    recipe_path = Path(__file__).with_name("macos-arm64.json")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (directory / "manifest.json").exists() or (directory / "disk.raw").exists():
        parser.error("Use a new build directory; an existing image is never overwritten.")
    cache = directory / "downloads"
    payload = directory / "payload"
    if cache.is_symlink():
        parser.error("The download cache must not be a symbolic link.")
    cache.mkdir(mode=0o700, exist_ok=True)
    payload.mkdir(mode=0o700)
    wheel = args.wheel.resolve(strict=True)
    requirements = args.requirements.resolve(strict=True)
    shutil.copyfile(wheel, payload / wheel.name)
    shutil.copyfile(requirements, payload / "requirements.txt")
    base = cache / "debian-arm64.tar.xz"
    helper = directory / "vfkit"
    download(recipe["base_url"], base, recipe["base_sha512"], "sha512")
    download(recipe["helper_url"], helper, recipe["helper_sha256"], "sha256")
    helper.chmod(0o755)
    license_text = urllib.request.urlopen(recipe["helper_license_url"], timeout=30).read()
    (directory / "VFKIT-LICENSE").write_bytes(license_text)
    helper_notice = (
        "vfkit v" + recipe["helper_version"] + " (the Mac hypervisor helper): Apache License 2.0; "
        "see VFKIT-LICENSE.\n\n"
    )
    with tarfile.open(base, "r:xz") as archive:
        member = archive.getmember("disk.raw")
        archive.extract(member, directory, filter="data")
    with (directory / "disk.raw").open("r+b") as disk:
        disk.truncate(recipe["disk_bytes"])
    inventory = {
        "schema": 1,
        "wheel_sha256": digest(wheel),
        "requirements_sha256": digest(requirements),
        "recipe_sha256": digest(recipe_path),
        "builder_sha256": digest(Path(__file__)),
    }
    agent = (ROOT / "src/alysis_code/bots/vm_guest.py").read_text(encoding="utf-8")
    service = """[Unit]
Description=Alysis host-only computer bridge
After=systemd-modules-load.service
[Service]
ExecStart=/usr/bin/python3 -I /usr/local/lib/alysis-vm-guest.py
Restart=on-failure
UMask=0077
[Install]
WantedBy=multi-user.target
"""
    setup = (
        """set -eu
systemctl disable --now ssh.service ssh.socket || true
install -d /mnt/payload /opt/alysis /var/lib/alysis
mount -t virtiofs -o ro alysis-payload /mnt/payload
python3 -m venv /opt/alysis/.venv
/opt/alysis/.venv/bin/python -m pip --isolated install --no-cache-dir --require-hashes -r /mnt/payload/requirements.txt
/opt/alysis/.venv/bin/python -m pip --isolated install --no-cache-dir --no-deps /mnt/payload/"""
        + wheel.name
        + """
# Owl commands run this environment's python3: let it also import OS-packaged modules such as
# LibreOffice's uno, after its own pinned packages.
/opt/alysis/.venv/bin/python -c "import pathlib,site; pathlib.Path(site.getsitepackages()[0],'zz-debian-dist-packages.pth').write_text('/usr/lib/python3/dist-packages\\n')"
/opt/alysis/.venv/bin/python - <<'GUEST'
import importlib.metadata,json,pathlib,subprocess
p=pathlib.Path('/var/lib/alysis/image-manifest.json')
x=json.loads(p.read_text())
x['python_packages']=sorted((d.metadata.get('Name',''),d.version) for d in importlib.metadata.distributions())
x['system_packages']=subprocess.check_output(['dpkg-query','-W','-f=${Package}=${Version}\\n'],text=True).splitlines()
p.write_text(json.dumps(x))
GUEST
# Third-party notices and source references for everything installed (license_pack.py).
"""
        + license_pack.setup_step()
        + """chown -R root:root /opt/alysis
chmod -R go-w /opt/alysis
# The builder and each task receive a different virtual NIC identity. Do not
# retain cloud-init's MAC-specific match from the image-building computer.
rm -f /etc/netplan/50-cloud-init.yaml
install -d /etc/systemd/network
cat > /etc/systemd/network/00-alysis-computer.network <<'NETWORK'
[Match]
Type=ether
[Network]
DHCP=yes
NETWORK
systemctl enable systemd-networkd systemd-resolved
rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub /var/lib/systemd/random-seed
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
touch /etc/cloud/cloud-init.disabled
systemctl daemon-reload
systemctl enable --now alysis-vm-guest.service
printf ready > /var/lib/alysis/image-ready
sync
"""
    )
    config = {
        "users": [
            {"name": "alysis", "uid": 1000, "lock_passwd": True, "shell": "/usr/sbin/nologin"}
        ],
        "ssh_pwauth": False,
        "disable_root": True,
        "growpart": {"mode": "auto", "devices": ["/"]},
        "resize_rootfs": True,
        "package_update": True,
        "packages": [
            "python3-venv",
            "bubblewrap",
            "xvfb",
            "x11-utils",
            "xdotool",
            "xclip",
            "openbox",
            "tint2",
            "hsetroot",
            "pcmanfm",
            "xterm",
            "adwaita-icon-theme",
            "libreoffice-writer",
            "libreoffice-calc",
            "libreoffice-impress",
            # PDF text and page tools for owls (Office files use the engine's document extra).
            "poppler-utils",
            "fonts-dejavu",
            "fonts-noto-core",
            "fonts-noto-cjk",
            "fonts-noto-color-emoji",
            "chromium",
            "chromium-sandbox",
            "dbus-x11",
            "libnss3-tools",
            "ca-certificates",
            "xauth",
        ],
        "write_files": [
            {"path": "/usr/local/lib/alysis-vm-guest.py", "permissions": "0600", "content": agent},
            {
                "path": "/etc/systemd/system/alysis-vm-guest.service",
                "permissions": "0644",
                "content": service,
            },
            {"path": "/usr/local/sbin/alysis-image-setup", "permissions": "0700", "content": setup},
            {
                "path": license_pack.GUEST_SCRIPT,
                "permissions": "0700",
                "content": license_pack.guest_script(),
            },
            {
                "path": "/var/lib/alysis/image-manifest.json",
                "permissions": "0644",
                "content": json.dumps(inventory),
            },
        ],
        "runcmd": [["bash", "/usr/local/sbin/alysis-image-setup"]],
    }
    (directory / "user-data").write_text("#cloud-config\n" + json.dumps(config))
    (directory / "meta-data").write_text(
        "instance-id: alysis-build-" + uuid4().hex + "\nlocal-hostname: alysis-computer\n"
    )
    with tempfile.TemporaryDirectory(prefix="alysis-image-") as transport:
        endpoint = Path(transport) / "computer.sock"
        argv = [
            str(helper),
            "--cpus",
            "2",
            "--memory",
            "3072",
            "--bootloader",
            f"efi,variable-store={directory / 'nvram'},create",
            "--device",
            f"virtio-blk,path={directory / 'disk.raw'}",
            "--device",
            "virtio-net,nat",
            "--device",
            "virtio-rng",
            "--device",
            f"virtio-vsock,port=10789,socketURL={endpoint},connect",
            "--device",
            f"virtio-fs,sharedDir={payload},mountTag=alysis-payload",
            "--device",
            f"virtio-serial,logFilePath={directory / 'serial.log'}",
            "--cloud-init",
            f"{directory / 'user-data'},{directory / 'meta-data'}",
            "--log-level",
            "error",
        ]
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        )
        try:
            deadline = time.monotonic() + 1800
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if rpc(endpoint, {"operation": "ping"}, timeout=2).get("ready"):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Image setup did not finish; inspect the credential-free serial log."
                )
            result = rpc(
                endpoint,
                {
                    "operation": "run",
                    "argv": ["/usr/bin/cat", "/var/lib/alysis/image-manifest.json"],
                    "timeout": 10,
                },
            )
            if result["exit_code"] != 0:
                raise RuntimeError("The built image returned no software inventory.")
            (directory / "inventory.json").write_text(result["stdout"])
            probe = rpc(
                endpoint,
                {
                    "operation": "run",
                    "argv": [
                        "/opt/alysis/.venv/bin/python",
                        "-I",
                        "-c",
                        "from alysis_code.bots.desktop_linux import available; raise SystemExit(0 if available() else 1)",
                    ],
                    "timeout": 30,
                },
                timeout=35,
            )
            if probe["exit_code"] != 0:
                raise RuntimeError(
                    "The private Linux display and namespace did not pass their real startup check."
                )
            rpc(endpoint, {"operation": "run", "argv": ["/usr/bin/sync"], "timeout": 10})
            try:
                rpc(endpoint, {"operation": "shutdown"})
            except (OSError, ValueError):
                pass  # Confirm the actual hypervisor exit below, never replay shutdown.
            process.wait(timeout=45)
            if process.returncode != 0:
                raise RuntimeError("The image-building VM did not stop cleanly.")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=8)
    licenses = license_pack.copy_from_disk(directory / "disk.raw", directory, prefix=helper_notice)
    manifest = {
        "schema": 1,
        "architecture": "arm64",
        "qualification": "private-test",
        "hypervisor_version": recipe["helper_version"],
        "helper": {"name": "vfkit", "sha256": digest(helper)},
        "image": {"name": "disk.raw", "sha256": digest(directory / "disk.raw")},
        "license_pack": licenses,
        **inventory,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "image_ready": True,
                "directory": str(directory),
                "disk_bytes": recipe["disk_bytes"],
                "wheel_sha256": inventory["wheel_sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
