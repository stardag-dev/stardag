"""Tests for settings, in particular database URL handling."""

from stardag_api.config import Settings


def test_effective_database_url_from_parts():
    settings = Settings(
        database_host="db.example.com",
        database_port=5432,
        database_name="stardag",
        database_user="user",
        database_password="pass",
    )
    assert (
        settings.effective_database_url
        == "postgresql+asyncpg://user:pass@db.example.com:5432/stardag"
    )


def test_effective_database_url_explicit_wins():
    settings = Settings(database_url="postgresql+asyncpg://u:p@pooled-host/db")
    assert settings.effective_database_url == "postgresql+asyncpg://u:p@pooled-host/db"


def test_migration_url_falls_back_to_database_url():
    settings = Settings(database_url="postgresql+asyncpg://u:p@pooled-host/db")
    assert (
        settings.effective_migration_database_url
        == "postgresql+asyncpg://u:p@pooled-host/db"
    )


def test_migration_url_prefers_direct():
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@pooled-host/db",
        database_url_direct="postgresql+asyncpg://u:p@direct-host/db",
    )
    assert (
        settings.effective_migration_database_url
        == "postgresql+asyncpg://u:p@direct-host/db"
    )


def test_pooler_compat_default_off():
    assert Settings().database_pooler_compat is False
