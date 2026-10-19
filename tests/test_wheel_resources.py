from pathlib import Path

from voice_input import resources


def test_installed_wheel_prefers_its_bundled_resources(tmp_path, monkeypatch):
    module = tmp_path / "site-packages" / "voice_input" / "resources.py"
    bundled = module.parent / "bundled"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(resources, "__file__", str(module))
    monkeypatch.setattr(resources.sys, "frozen", False, raising=False)
    assert resources.app_base_dir() == bundled


def test_source_checkout_keeps_repository_resource_lookup(tmp_path, monkeypatch):
    module = tmp_path / "src" / "voice_input" / "resources.py"
    monkeypatch.setattr(resources, "__file__", str(module))
    monkeypatch.setattr(resources.sys, "frozen", False, raising=False)
    assert resources.app_base_dir() == Path(tmp_path)
