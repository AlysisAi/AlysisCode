"""Build the credential-free, Docker-free private Windows Bot computer image.

The Windows twin of ``build_macos_image.py``: same Debian build date, packages, guest agent and
sealing steps, booted through the installed Alysis Owls computer service (``alysis-owls-computer``
in the app repo) exactly as task computers are. Supply an internal engine wheel and uv-lock-exported
hashed requirements with the ``documents`` extra (``image_requirements.py``). Only the image-building VM receives them (uploaded through the guest agent);
task VMs get no host files at all.

Output directory: ``disk.vhdx`` (dynamic, sealed), ``disk.vhdx.gz`` (its download), ``vmlinuz``,
``initrd.img``, ``manifest.json`` (sha256 of each, root PARTUUID, downloads, inventory),
``inventory.json`` and ``THIRD-PARTY-NOTICES.txt``. Publish everything except ``disk.vhdx`` under
``https://updates.alysiscode.com/owls-computer/windows-x64/<version>/`` and put the printed
``version`` and ``manifest_sha256`` into the app's ``WINDOWS_COMPUTER_IMAGE``.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import gzip
import hashlib
import json
import platform
import shutil
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import disk_formats  # noqa: E402
import image_requirements  # noqa: E402
import license_pack  # noqa: E402

from alysis_code.bots import local_hcs  # noqa: E402

SEED_BYTES = 8 * 1024 * 1024
PAYLOAD = "/tmp/alysis-cloud-payload"


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


def to_dynamic_vhdx(source: Path, target: Path):
    """Copy a fixed VHD into a dynamic VHDX; all-zero blocks are not stored. No admin needed."""
    virtdisk = ctypes.WinDLL("virtdisk", use_last_error=True)
    storage = local_hcs._StorageType(local_hcs._VHDX)
    ctypes.memmove(storage.vendor_id.raw, local_hcs._MICROSOFT_VENDOR.bytes_le, 16)
    parameters = local_hcs._CreateParameters()
    parameters.version = 2
    parameters.version2.source_path = str(source)
    handle = ctypes.c_void_p()
    error = virtdisk.CreateVirtualDisk(
        ctypes.byref(storage),
        ctypes.c_wchar_p(str(target)),
        0,
        None,
        0,
        0,
        ctypes.byref(parameters),
        None,
        ctypes.byref(handle),
    )
    if handle.value:
        ctypes.WinDLL("kernel32").CloseHandle(handle)
    if error:
        raise RuntimeError(f"Windows could not write the sealed disk ({error}).")


def boot_files(disk: Path):
    with disk.open("rb") as stream:
        offset, _size, partuuid = disk_formats.linux_root(stream)
        version, kernel, initrd = disk_formats.newest_boot_files(disk_formats.Ext4(stream, offset))
    return partuuid, version, kernel, initrd


def guest(runtime_id, payload, timeout=10):
    reply = local_hcs._guest_call(runtime_id, payload, timeout)
    if reply.get("ok") is not True:
        raise RuntimeError("Image builder operation failed.")
    return reply["result"]


def guest_run(runtime_id, argv, timeout=10):
    return guest(runtime_id, {"operation": "run", "argv": argv, "timeout": timeout}, timeout + 5)


def wait_for(description, deadline_s, probe):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(2)
    raise RuntimeError(f"Image setup did not reach: {description}.")


def setup_script(wheel_name: str) -> str:
    return (
        """set -eu
systemctl disable --now ssh.service ssh.socket || true
install -d /opt/alysis /var/lib/alysis
install -d -m 0700 -o 1000 -g 1000 """
        + PAYLOAD
        + """
modprobe hv_sock || true
systemctl daemon-reload
systemctl enable --now alysis-vm-guest.service
# The builder uploads the wheel and requirements through the guest agent, then "ready".
for _ in $(seq 1 1800); do [ -f """
        + PAYLOAD
        + """/ready ] && break; sleep 1; done
test -f """
        + PAYLOAD
        + """/ready
install -d -m 0700 /root/payload
cp """
        + PAYLOAD
        + """/requirements.txt """
        + PAYLOAD
        + "/"
        + wheel_name
        + """ /root/payload/
rm -rf """
        + PAYLOAD
        + """
python3 -m venv /opt/alysis/.venv
/opt/alysis/.venv/bin/python -m pip --isolated install --no-cache-dir --require-hashes -r /root/payload/requirements.txt
/opt/alysis/.venv/bin/python -m pip --isolated install --no-cache-dir --no-deps /root/payload/"""
        + wheel_name
        + """
rm -rf /root/payload
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
# Task computers receive their address and DNS on the kernel command line from the Alysis
# service (systemd-network-generator), not DHCP, and never cloud-init's builder-specific match.
rm -f /etc/netplan/50-cloud-init.yaml
systemctl enable systemd-network-generator.service systemd-networkd systemd-resolved
rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub /var/lib/systemd/random-seed
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
touch /etc/cloud/cloud-init.disabled
apt-get clean
# Zero free space so the sealed dynamic disk (and its download) stores only real data.
dd if=/dev/zero of=/var/tmp/alysis-zero bs=4M status=none || true
rm -f /var/tmp/alysis-zero
systemctl daemon-reload
printf ready > /var/lib/alysis/image-ready
sync
"""
    )


def cloud_config(agent: str, service: str, setup: str, inventory: dict) -> dict:
    return {
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
            # Hyper-V's vsock transport (the Mac image uses virtio's, which loads on its own).
            {
                "path": "/etc/modules-load.d/alysis-vsock.conf",
                "permissions": "0644",
                "content": "hv_sock\n",
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
    if sys.platform != "win32" or platform.machine() != "AMD64":
        parser.error("This qualified image recipe requires 64-bit Windows on x86-64.")
    recipe_path = Path(__file__).with_name("windows-x64.json")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "manifest.json").exists() or (directory / "disk.vhdx").exists():
        parser.error("Use a new build directory; an existing image is never overwritten.")
    cache = directory / "downloads"
    if cache.is_symlink():
        parser.error("The download cache must not be a symbolic link.")
    cache.mkdir(exist_ok=True)
    wheel = args.wheel.resolve(strict=True)
    requirements = args.requirements.resolve(strict=True)
    if wheel.stat().st_size > 50 * 1024 * 1024 or requirements.stat().st_size > 50 * 1024 * 1024:
        parser.error("The wheel and requirements must each be at most 50 MiB.")
    base = cache / "debian-amd64.tar.xz"
    download(recipe["base_url"], base, recipe["base_sha512"], "sha512")
    build_disk = directory / "build.vhd"
    with tarfile.open(base, "r:xz") as archive:
        archive.extract(archive.getmember("disk.raw"), directory, filter="data")
    with (directory / "disk.raw").open("r+b") as disk:
        disk.truncate(recipe["disk_bytes"])
        disk.seek(recipe["disk_bytes"])
        disk.write(disk_formats.vhd_footer(recipe["disk_bytes"]))
    (directory / "disk.raw").rename(build_disk)
    partuuid, version, kernel, initrd = boot_files(build_disk)
    (directory / "build-vmlinuz").write_bytes(kernel)
    (directory / "build-initrd.img").write_bytes(initrd)
    inventory = {
        "schema": 1,
        "wheel_sha256": digest(wheel),
        "requirements_sha256": digest(requirements),
        "recipe_sha256": digest(recipe_path),
        "builder_sha256": digest(Path(__file__)),
        "base_kernel": version,
    }
    agent = (ROOT / "src/alysis_code/bots/vm_guest.py").read_text(encoding="utf-8")
    service = (
        "[Unit]\nDescription=Alysis host-only computer bridge\nAfter=systemd-modules-load.service\n"
        "[Service]\nExecStart=/usr/bin/python3 -I /usr/local/lib/alysis-vm-guest.py\nRestart=on-failure\nUMask=0077\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    config = cloud_config(agent, service, setup_script(wheel.name), inventory)
    seed = directory / "seed.vhd"
    volume = disk_formats.fat12_volume(
        {
            "user-data": ("#cloud-config\n" + json.dumps(config)).encode(),
            "meta-data": f"instance-id: alysis-build-{uuid.uuid4().hex}\nlocal-hostname: alysis-computer\n".encode(),
            # No cloud-init network setting: the address comes from the service on the kernel
            # command line, and a builder-specific DHCP setting would override it.
            "network-config": json.dumps({"version": 2, "ethernets": {}}).encode(),
        }
    )
    with seed.open("wb") as stream:
        stream.write(volume)
        stream.truncate(SEED_BYTES)
        stream.seek(SEED_BYTES)
        stream.write(disk_formats.vhd_footer(SEED_BYTES))
    vm_id = str(uuid.uuid4())
    for path in (build_disk, seed, directory / "build-vmlinuz", directory / "build-initrd.img"):
        local_hcs._grant_vm(vm_id, path)
    session = local_hcs._ServiceSession()
    created = session.call(
        {
            "op": "create",
            "vm_id": vm_id,
            "kernel": str(directory / "build-vmlinuz"),
            "initrd": str(directory / "build-initrd.img"),
            "root": f"PARTUUID={partuuid}",
            "disks": [{"path": str(build_disk)}, {"path": str(seed), "read_only": True}],
            "memory_mib": 3072,
            "cpus": 2,
            "internet": True,
        }
    )
    runtime_id = created["runtime_id"]
    try:
        wait_for(
            "the guest agent", 1800, lambda: "ready" in guest(runtime_id, {"operation": "ping"}, 3)
        )
        wait_for(
            "the payload folder",
            300,
            lambda: guest_run(runtime_id, ["/usr/bin/test", "-d", PAYLOAD])["exit_code"] == 0,
        )
        for name, data in (
            (requirements.name, requirements.read_bytes()),
            (wheel.name, wheel.read_bytes()),
        ):
            target = "requirements.txt" if name == requirements.name else wheel.name
            guest(
                runtime_id,
                {
                    "operation": "upload",
                    "path": f"{PAYLOAD}/{target}",
                    "size": len(data),
                    "data": base64.b64encode(data).decode(),
                },
                120,
            )
        guest(
            runtime_id,
            {"operation": "upload", "path": f"{PAYLOAD}/ready", "size": 0, "data": ""},
            30,
        )
        wait_for(
            "a finished image",
            1800,
            lambda: guest(runtime_id, {"operation": "ping"}, 3).get("ready") is True,
        )
        result = guest_run(runtime_id, ["/usr/bin/cat", "/var/lib/alysis/image-manifest.json"])
        if result["exit_code"] != 0:
            raise RuntimeError("The built image returned no software inventory.")
        (directory / "inventory.json").write_text(result["stdout"])
        probe = guest_run(
            runtime_id,
            [
                "/opt/alysis/.venv/bin/python",
                "-I",
                "-c",
                "from alysis_code.bots.desktop_linux import available; raise SystemExit(0 if available() else 1)",
            ],
            30,
        )
        if probe["exit_code"] != 0:
            raise RuntimeError(
                "The private Linux display and namespace did not pass their real startup check."
            )
        network = guest_run(
            runtime_id,
            [
                "/opt/alysis/.venv/bin/python",
                "-I",
                "-c",
                "import urllib.request as u; u.urlopen('https://deb.debian.org/debian/', timeout=20)",
            ],
            30,
        )
        if network["exit_code"] != 0:
            raise RuntimeError(
                "The image-building VM could not reach the internet over its service network."
            )
        guest_run(runtime_id, ["/usr/bin/sync"])
        try:
            guest(runtime_id, {"operation": "shutdown"})
        except (OSError, ValueError, RuntimeError):
            pass  # Confirm the actual stop below, never replay shutdown.
        wait_for(
            "a clean power-off",
            90,
            lambda: session.call({"op": "state", "vm_id": vm_id})["state"] != "running",
        )
    finally:
        try:
            session.call({"op": "stop", "vm_id": vm_id})
        except (KeyError, local_hcs.ComputeError):
            pass
        session.close()
    partuuid_after, version_after, kernel, initrd = boot_files(build_disk)
    if partuuid_after != partuuid:
        raise RuntimeError("The root partition changed identity during setup.")
    licenses = license_pack.copy_from_disk(build_disk, directory)
    (directory / "vmlinuz").write_bytes(kernel)
    (directory / "initrd.img").write_bytes(initrd)
    to_dynamic_vhdx(build_disk, directory / "disk.vhdx")
    for leftover in (build_disk, seed, directory / "build-vmlinuz", directory / "build-initrd.img"):
        leftover.unlink(missing_ok=True)
    compressed = directory / "disk.vhdx.gz"
    with (
        (directory / "disk.vhdx").open("rb") as source,
        gzip.open(compressed, "wb", compresslevel=6) as target,
    ):
        shutil.copyfileobj(source, target, 4 * 1024 * 1024)
    manifest = {
        "schema": 1,
        "platform": "windows",
        "architecture": "AMD64",
        "qualification": "private-test",
        "root": f"PARTUUID={partuuid}",
        "kernel_version": version_after,
        "image": {"name": "disk.vhdx", "sha256": digest(directory / "disk.vhdx")},
        "kernel": {"name": "vmlinuz", "sha256": digest(directory / "vmlinuz")},
        "initrd": {"name": "initrd.img", "sha256": digest(directory / "initrd.img")},
        # The app downloads the compressed disk and verifies both the download and the result.
        "downloads": {
            "disk.vhdx": {
                "name": compressed.name,
                "sha256": digest(compressed),
                "encoding": "gzip",
                "size": compressed.stat().st_size,
            }
        },
        "license_pack": licenses,
        **inventory,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    version = time.strftime("%Y%m%d", time.gmtime()) + "-" + inventory["wheel_sha256"][:12]
    print(
        json.dumps(
            {
                "image_ready": True,
                "directory": str(directory),
                "version": version,
                "manifest_sha256": digest(directory / "manifest.json"),
                "download_bytes": compressed.stat().st_size,
                "disk_vhdx_bytes": (directory / "disk.vhdx").stat().st_size,
                "wheel_sha256": inventory["wheel_sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
