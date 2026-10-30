"""Reading feature metadata must not initialize unrelated implementations."""

import os
import subprocess
import sys
from pathlib import Path


def test_skill_discovery_preserves_lazy_public_exports(tmp_path: Path):
    code = """
import importlib, inspect, pickle, sys
from alysis_code.config import AppConfig
from alysis_code.skills import resolve_skills_enabled
assert resolve_skills_enabled(AppConfig())
for name in ('skills.install', 'skills.scaffold', 'skills.transactions'):
    assert 'alysis_code.' + name not in sys.modules, name
# Discovery still validates bundles before admitting them.
assert 'alysis_code.skills.validation' in sys.modules
# Explicit public imports still return the original classes and functions.
for package_name, exports in (
    ('skills', {'install': ('SkillInstallResult', 'install_skill_bundle',
                           'SkillRemoveResult', 'remove_managed_skill'),
                'scaffold': ('SkillScaffoldResult', 'scaffold_skill_bundle'),
                'validation': ('SkillValidationIssue', 'SkillValidationResult',
                               'validate_skill_bundle')}),
):
    package = importlib.import_module('alysis_code.' + package_name)
    for module_name, names in exports.items():
        for name in names:
            assert name in dir(package)
            value = getattr(package, name)
            canonical = getattr(importlib.import_module(package.__name__ + '.' + module_name), name)
            assert value is canonical, name
            assert inspect.signature(value) == inspect.signature(canonical), name
            assert pickle.loads(pickle.dumps(value)) is value, name
    public = {}
    exec('from ' + package.__name__ + ' import *', public)
    for name in package.__all__:
        assert public[name] is getattr(package, name), name
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
            ALYSIS_DATA_DIR=str(tmp_path / "data"),
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
