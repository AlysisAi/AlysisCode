"""The owl computer image ships with every third-party notice and a source reference."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bots-local"))

import image_notices  # noqa: E402
import license_pack  # noqa: E402

GPL_FILE = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: tool

Files: *
Copyright: 2024 Upstream Authors
License: GPL-2+

Files: debian/*
License: GPL-2+
 On Debian systems, see /usr/share/common-licenses/GPL-2.
"""


def doc_tree(root: Path, files: dict[str, str]) -> Path:
    for package, text in files.items():
        (root / "doc" / package).mkdir(parents=True)
        (root / "doc" / package / "copyright").write_text(text, encoding="utf-8")
    (root / "common").mkdir()
    (root / "common" / "GPL-2").write_text("GNU GENERAL PUBLIC LICENSE Version 2\n")
    (root / "common" / "Apache-2.0").write_text("Apache License 2.0\n")
    return root


def test_notices_group_shared_texts_and_reference_every_source(tmp_path):
    root = doc_tree(
        tmp_path,
        {
            "libtool1": GPL_FILE,
            "tool-bin": GPL_FILE,
            "fonts-ελληνικά": "Copyright © Ελληνικά 日本語\nLicense: OFL-1.1\n",
        },
    )
    packages = image_notices.debian_packages(
        [
            "tool-bin\t1:2.0+dfsg-1~deb13u1\ttool\t1:2.0+dfsg-1~deb13u1\tamd64",
            "libtool1\t1:2.0+dfsg-1~deb13u1+b1\ttool\t1:2.0+dfsg-1~deb13u1\tamd64",
            "fonts-ελληνικά\t3.0-1\t\t\tall",
        ]
    )
    python = [
        {
            "name": "Requests",
            "version": "2.32.0",
            "license": "Apache-2.0",
            "classifiers": [],
            "files": [("requests-2.32.0.dist-info/licenses/LICENSE", "Apache text")],
        },
        {
            "name": "tiny",
            "version": "0.1",
            "license": "",
            "classifiers": ["MIT License"],
            "files": [],
        },
    ]
    notices, sources = image_notices.build(packages, root / "doc", root / "common", python)

    assert notices.count("Packages: libtool1") == 1  # one block for the shared text
    assert "libtool1 1:2.0+dfsg-1~deb13u1+b1, tool-bin 1:2.0+dfsg-1~deb13u1" in notices
    assert "Licenses: GPL-2+" in notices
    assert "GNU GENERAL PUBLIC LICENSE Version 2" in notices  # referenced common text
    assert "Apache License 2.0\n" not in notices  # unreferenced common text is not padded in
    assert "Copyright © Ελληνικά 日本語" in notices
    assert "Requests 2.32.0" in notices and "Apache text" in notices
    assert "License: MIT License" in notices and "see its source on PyPI" in notices
    tool = next(item for item in sources["debian"] if item["source"] == "tool")
    assert tool["binaries"] == ["libtool1", "tool-bin"]
    assert tool["url"] == "https://snapshot.debian.org/package/tool/1%3A2.0%2Bdfsg-1~deb13u1/"
    assert {item["source"] for item in sources["debian"]} == {"tool", "fonts-ελληνικά"}
    assert sources["python"][0]["url"] == "https://pypi.org/project/Requests/2.32.0/#files"


def test_a_package_without_a_copyright_file_stops_the_build(tmp_path):
    root = doc_tree(tmp_path, {"documented": GPL_FILE})
    packages = image_notices.debian_packages(["documented\t1\t\t\tall", "undocumented\t2\t\t\tall"])
    with pytest.raises(SystemExit, match="undocumented"):
        image_notices.build(packages, root / "doc", root / "common", [])


def test_vendor_notices_are_preserved_without_inventing_a_debian_source(tmp_path):
    root = doc_tree(
        tmp_path, {"vendor-browser": "Copyright browser contributors\nActual credits\n"}
    )
    packages = image_notices.debian_packages(["vendor-browser\t1\t\t\tarm64"])
    notices, sources = image_notices.build(
        packages,
        root / "doc",
        root / "common",
        [],
        {"vendor-browser": "https://vendor.example/browser"},
    )
    assert "Actual credits" in notices
    assert sources["debian"] == []
    assert sources["vendors"] == [
        {"name": "vendor-browser", "version": "1", "url": "https://vendor.example/browser"}
    ]


class FakeFilesystem:
    def __init__(self, files):
        self.files = files

    def lookup(self, path):
        if path not in self.files:
            raise KeyError(path)
        return path

    def read(self, number):
        return self.files[number]


@pytest.fixture
def disk(tmp_path, monkeypatch):
    files = {
        "/usr/share/alysis/THIRD-PARTY-NOTICES.txt": "Notices Ελληνικά\n".encode(),
        "/usr/share/alysis/sources.json": b'{"schema": 1, "debian": [], "python": []}\n',
    }
    monkeypatch.setattr(license_pack.disk_formats, "linux_root", lambda stream: (0, 0, "x"))
    monkeypatch.setattr(
        license_pack.disk_formats, "Ext4", lambda stream, offset: FakeFilesystem(files)
    )
    path = tmp_path / "disk.raw"
    path.write_bytes(b"\0")
    out = tmp_path / "out"
    out.mkdir()
    return path, out, files


def test_the_pack_is_copied_off_the_disk_with_its_checksums(disk):
    path, out, _ = disk
    entries = license_pack.copy_from_disk(path, out, prefix="Helper notice\n\n")
    notices = (out / "THIRD-PARTY-NOTICES.txt").read_bytes()
    assert notices == "Helper notice\n\nNotices Ελληνικά\n".encode()
    assert entries["notices"]["sha256"] == hashlib.sha256(notices).hexdigest()
    assert json.loads((out / "sources.json").read_text())["schema"] == 1


@pytest.mark.parametrize("damage", ["missing", "not json", "empty"])
def test_a_missing_or_damaged_pack_is_never_published(disk, damage):
    path, out, files = disk
    if damage == "missing":
        del files["/usr/share/alysis/sources.json"]
    elif damage == "not json":
        files["/usr/share/alysis/sources.json"] = b"{broken"
    else:
        files["/usr/share/alysis/THIRD-PARTY-NOTICES.txt"] = b"   \n"
    with pytest.raises((RuntimeError, ValueError)):
        license_pack.copy_from_disk(path, out)


def test_the_windows_image_runs_the_notices_step_after_its_python_packages():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import build_windows_image

    setup = build_windows_image.setup_script("engine.whl")
    assert setup.index("install --no-cache-dir --no-deps") < setup.index(license_pack.GUEST_SCRIPT)
    config = build_windows_image.cloud_config("agent", "service", setup, {})
    shipped = {item["path"]: item["content"] for item in config["write_files"]}
    assert shipped[license_pack.GUEST_SCRIPT] == license_pack.guest_script()
    chrome = "/usr/local/sbin/alysis-install-chrome"
    assert "google-chrome-stable" in shipped[chrome]
    assert setup.index("bash " + chrome) < setup.index(license_pack.GUEST_SCRIPT)
    assert "x11vnc" in config["packages"]


def test_the_windows_image_records_google_chromes_notices_before_the_notices_step():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import build_windows_image

    setup = build_windows_image.setup_script("engine.whl")
    notice = "/usr/share/doc/google-chrome-stable/copyright"
    installed = setup.index("bash /usr/local/sbin/alysis-install-chrome")
    recorded = setup.index("--dump-dom chrome://credits > " + notice)
    assert installed < recorded < setup.index(license_pack.GUEST_SCRIPT)
    assert "test -s " + notice in setup
    config = build_windows_image.cloud_config("agent", "service", setup, {})
    shipped = {item["path"]: item["content"] for item in config["write_files"]}
    assert json.loads(shipped["/var/lib/alysis/vendor-sources.json"]) == {
        "google-chrome-stable": "https://www.google.com/chrome/"
    }


def test_the_windows_and_mac_images_record_chromes_notices_the_same_way():
    recipes = Path(__file__).resolve().parents[1] / "scripts" / "bots-local"

    def steps(name):
        text = (recipes / name).read_text(encoding="utf-8")
        return [
            line.strip()
            for line in text.splitlines()
            if "google-chrome-stable/copyright" in line or "alysis-chrome-notices" in line
        ]

    assert steps("build_windows_image.py") == steps("build_macos_image.py") != []


class Archive:
    """Snapshot and PyPI stand-ins: url -> bytes, counting downloads."""

    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, url):
        import io
        from contextlib import closing

        self.calls.append(url)
        if url not in self.files:
            import urllib.error

            raise urllib.error.HTTPError(url, 404, "missing", None, None)
        return closing(io.BytesIO(self.files[url]))


def test_sources_are_downloaded_verified_and_resumed(tmp_path):
    import fetch_image_sources as fetch

    dsc, orig = b"Format: 3.0", b"orig tarball bytes"
    sha1 = {name: hashlib.sha1(data).hexdigest() for name, data in (("dsc", dsc), ("orig", orig))}
    sdist = b"sdist bytes"
    listing = {
        "result": [{"hash": sha1["dsc"]}, {"hash": sha1["orig"]}],
        "fileinfo": {
            sha1["dsc"]: [{"name": "tool_2.0+dfsg-1.dsc"}],
            sha1["orig"]: [{"name": "tool_2.0+dfsg.orig.tar.xz"}],
        },
    }
    release = {
        "urls": [
            {
                "packagetype": "sdist",
                "filename": "six-1.16.0.tar.gz",
                "url": "https://files/six",
                "digests": {"sha256": hashlib.sha256(sdist).hexdigest()},
            }
        ]
    }
    archive = Archive(
        {
            "https://snapshot.debian.org/mr/package/tool/1%3A2.0%2Bdfsg-1/srcfiles?fileinfo=1": json.dumps(
                listing
            ).encode(),
            f"https://snapshot.debian.org/file/{sha1['dsc']}": dsc,
            f"https://snapshot.debian.org/file/{sha1['orig']}": orig,
            "https://pypi.org/pypi/six/1.16.0/json": json.dumps(release).encode(),
            "https://files/six": sdist,
            "https://pypi.org/pypi/wheels-only/1.0/json": json.dumps(
                {"urls": [{"packagetype": "bdist_wheel"}]}
            ).encode(),
        }
    )
    sources = {
        "debian": [{"source": "tool", "version": "1:2.0+dfsg-1"}],
        "python": [{"name": "six", "version": "1.16.0"}, {"name": "wheels-only", "version": "1.0"}],
    }
    out = tmp_path / "sources"
    assert fetch.run(sources, out, archive, pause=0) == {"files": 3, "missing": 1}
    assert (out / "debian" / "tool_2.0+dfsg-1" / "tool_2.0+dfsg.orig.tar.xz").read_bytes() == orig
    assert (out / "python" / "six-1.16.0.tar.gz").read_bytes() == sdist
    (missing,) = json.loads((out / "missing.json").read_text(encoding="utf-8"))
    assert missing["package"] == "wheels-only" and "no source distribution" in missing["reason"]

    downloads = len(archive.calls)
    fetch.run(sources, out, archive, pause=0)  # resume: verified files are not downloaded again
    listings = {url for url in archive.files if url.endswith("fileinfo=1") or url.endswith("/json")}
    assert set(archive.calls[downloads:]) <= listings


def test_a_file_that_does_not_match_its_checksum_is_not_kept(tmp_path):
    import fetch_image_sources as fetch

    archive = Archive({"https://x/file": b"tampered"})
    target = tmp_path / "pkg.tar.xz"
    with pytest.raises(ValueError, match="checksum"):
        fetch.fetch(
            "https://x/file", target, "sha256", hashlib.sha256(b"original").hexdigest(), archive
        )
    assert not target.exists() and not target.with_name("pkg.tar.xz.part").exists()


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "..", ""])
def test_archive_file_names_cannot_leave_the_output_folder(name):
    import fetch_image_sources as fetch

    with pytest.raises(ValueError):
        fetch.safe_name(name)


def test_the_mac_image_is_published_as_a_verified_gzip_download(tmp_path):
    import gzip

    import build_macos_image as mac

    image = tmp_path / "disk.raw"
    with image.open("wb") as disk:
        disk.write(b"boot" * 1024)
        disk.truncate(3 * 1024 * 1024)
    helper = tmp_path / "vfkit"
    helper.write_bytes(b"helper")
    inventory = {"wheel_sha256": "a" * 64}
    manifest = mac.image_manifest({"helper_version": "0.6.4"}, helper, image, {}, inventory)

    assert manifest["platform"] == "macos" and manifest["architecture"] == "arm64"
    assert manifest["helper"] == {"name": "vfkit", "sha256": hashlib.sha256(b"helper").hexdigest()}
    assert manifest["image"]["sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    download = manifest["downloads"]["disk.raw"]
    compressed = tmp_path / download["name"]
    assert download["name"] == "disk.raw.gz" and download["encoding"] == "gzip"
    assert download["size"] == compressed.stat().st_size < image.stat().st_size // 100
    assert download["sha256"] == hashlib.sha256(compressed.read_bytes()).hexdigest()
    assert gzip.decompress(compressed.read_bytes()) == image.read_bytes()
    assert manifest["wheel_sha256"] == "a" * 64


def test_the_mac_notices_carry_vfkits_whole_license():
    import build_macos_image as mac

    notice = mac.helper_notice(
        {"helper_version": "0.6.4"}, b"Apache License\nVersion 2.0, January 2004\n"
    )
    assert notice.startswith("vfkit v0.6.4 (the Mac hypervisor helper): Apache License 2.0\n\n")
    assert "Version 2.0, January 2004" in notice and notice.endswith("\n\n")
