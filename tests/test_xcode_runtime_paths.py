from pathlib import Path

from alysis_code.macos_sandbox import _xcode_code_paths


def test_selected_xcode_grants_code_and_sdk_directories_without_bundle_or_application_data(
    tmp_path,
):
    applications = tmp_path / "Applications"
    developer = applications / "Xcode_26.6.app" / "Contents" / "Developer"
    code = developer / "usr"
    sdk = developer / "Platforms/MacOSX.platform/Developer/SDKs"
    code.mkdir(parents=True)
    sdk.mkdir(parents=True)
    frameworks = [
        developer.parent / "Frameworks",
        developer.parent / "SharedFrameworks",
        developer.parent / "PlugIns",
        developer / "Library/Frameworks",
        developer / "Library/PrivateFrameworks",
        developer / "Library/Xcode/PlugIns",
        developer / "Library/Xcode/PrivatePlugIns",
        developer / "Platforms/MacOSX.platform/Developer/usr",
        developer / "Platforms/MacOSX.platform/Developer/Library/Frameworks",
        developer / "Platforms/MacOSX.platform/Developer/Library/PrivateFrameworks",
        developer / "Platforms/MacOSX.platform/Developer/Library/Xcode/PlugIns",
        developer / "Platforms/MacOSX.platform/Developer/Library/Xcode/PrivatePlugIns",
    ]
    for path in frameworks:
        path.mkdir(parents=True)
    metadata = [
        developer.parent / "Info.plist",
        developer / "Platforms/MacOSX.platform/Info.plist",
        developer / "Toolchains/XcodeDefault.xctoolchain/ToolchainInfo.plist",
    ]
    for path in metadata:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public platform metadata")
    secret = developer / "private-settings"
    secret.write_text("synthetic")
    unrelated = applications / "Other.app"
    unrelated.mkdir()
    paths = _xcode_code_paths(developer, applications_root=applications)
    assert set(paths) == {
        code.resolve(),
        sdk.resolve(),
        *(p.resolve() for p in frameworks + metadata),
    }
    assert not any(secret.is_relative_to(p) or unrelated.is_relative_to(p) for p in paths)
    assert applications not in paths and developer not in paths


def test_unselected_or_non_bundle_developer_directory_grants_nothing(tmp_path):
    developer = tmp_path / "project" / "Contents" / "Developer"
    (developer / "usr").mkdir(parents=True)
    assert _xcode_code_paths(developer, applications_root=tmp_path) == ()
    assert _xcode_code_paths(Path("relative/Developer"), applications_root=tmp_path) == ()


def test_xcode_framework_alias_cannot_expose_data_outside_the_selected_bundle(
    tmp_path, require_symlinks
):
    applications = tmp_path / "Applications"
    developer = applications / "Xcode.app/Contents/Developer"
    developer.mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    (private / "credential").write_text("synthetic")
    for name in ("Frameworks", "SharedFrameworks", "PlugIns"):
        (developer.parent / name).symlink_to(private, target_is_directory=True)
    for relative in (
        "Library/Xcode/PrivatePlugIns",
        "Platforms/MacOSX.platform/Developer/Library/Xcode/PrivatePlugIns",
    ):
        link = developer / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(private, target_is_directory=True)
    (developer.parent / "Info.plist").symlink_to(private / "credential")
    assert _xcode_code_paths(developer, applications_root=applications) == ()
