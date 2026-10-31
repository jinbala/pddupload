import os

import pytest

from pdd_listing_automation import config
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import Platform, SourceProduct
from pdd_listing_automation.storage.database import connect, initialize
from pdd_listing_automation.storage.repositories import SourceProductRepository


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in list(os.environ):
        if key.startswith("PDD_AUTOMATION_"):
            monkeypatch.delenv(key)
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(config, "DEFAULT_ROOT_DIR", root)
    return root


def test_different_start_directories_reuse_listing_database(project_root, tmp_path, monkeypatch):
    (project_root / ".env").write_text(
        "PDD_AUTOMATION_HEADLESS=true\nPDD_AUTOMATION_TARGET_SHOP=目标店\n", encoding="utf-8",
    )
    monkeypatch.chdir(project_root)
    first = Settings.from_environment()
    first.ensure_directories()
    initialize(first.database_path)
    with connect(first.database_path) as db:
        SourceProductRepository(db).upsert_many([
            SourceProduct(source_goods_id="10001", source_row_key="r", title="已铺货商品",
                          source_platform=Platform.PINDUODUO, target_shop="目标店"),
        ])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("PDD_AUTOMATION_HOME=wrong-db\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    second = Settings.from_environment()
    assert second.database_path == first.database_path
    assert second.profile_dir == first.profile_dir
    assert second.artifacts_dir == first.artifacts_dir
    assert second.headless
    with connect(second.database_path) as db:
        assert SourceProductRepository(db).listed_goods_ids(Platform.PINDUODUO, "目标店") == {"10001"}
    assert not (elsewhere / "data").exists()
    assert not (elsewhere / "wrong-db").exists()


@pytest.mark.parametrize("home", ["", "  ", "relative-storage", "absolute"])
def test_home_override_has_stable_base(project_root, tmp_path, monkeypatch, home):
    if home == "absolute":
        expected = tmp_path / "shared-storage"
        home = str(expected)
    elif home.strip():
        expected = project_root / home
    else:
        expected = project_root
    monkeypatch.setenv("PDD_AUTOMATION_HOME", home)
    monkeypatch.chdir(tmp_path)
    first = Settings.from_environment()
    monkeypatch.chdir(project_root)
    second = Settings.from_environment()
    assert first.root_dir == second.root_dir == expected.resolve()


def test_project_env_selects_custom_home_and_external_env_wins(project_root, tmp_path, monkeypatch):
    (project_root / ".env").write_text("PDD_AUTOMATION_HOME=custom\n", encoding="utf-8")
    custom = project_root / "custom"
    custom.mkdir()
    (custom / ".env").write_text(
        "PDD_AUTOMATION_HEADLESS=true\nPDD_AUTOMATION_LOGIN_WAIT_SECONDS=123\n", encoding="utf-8",
    )
    monkeypatch.setenv("PDD_AUTOMATION_HEADLESS", "false")
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_environment()
    assert settings.root_dir == custom
    assert not settings.headless
    assert settings.login_wait_seconds == 123


def test_explicit_absolute_home_overrides_project_env(project_root, tmp_path, monkeypatch):
    (project_root / ".env").write_text("PDD_AUTOMATION_HOME=unused\n", encoding="utf-8")
    custom = tmp_path / "explicit"
    custom.mkdir()
    (custom / ".env").write_text("PDD_AUTOMATION_HEADLESS=true\n", encoding="utf-8")
    monkeypatch.setenv("PDD_AUTOMATION_HOME", str(custom))
    settings = Settings.from_environment()
    assert settings.root_dir == custom
    assert settings.headless
