"""
Unit tests for app/config.py — DATABASE_URL normalization.

A bare "postgres://" or "postgresql://" URL makes SQLAlchemy default to the
psycopg2 dialect, which requirements.txt does not install (psycopg3 instead —
see the comment on _normalize_db_url). This silently breaks app startup in
any environment whose Postgres URL isn't already suffixed with +psycopg, so
it's covered directly rather than only caught by a full docker-compose run.
"""
from app.config import _normalize_db_url


class TestNormalizeDbUrl:
    def test_bare_postgres_scheme_gets_psycopg_driver(self):
        url = _normalize_db_url("postgres://user:pw@host:5432/db")
        assert url == "postgresql+psycopg://user:pw@host:5432/db"

    def test_bare_postgresql_scheme_gets_psycopg_driver(self):
        url = _normalize_db_url("postgresql://user:pw@host:5432/db")
        assert url == "postgresql+psycopg://user:pw@host:5432/db"

    def test_already_suffixed_url_is_left_alone(self):
        url = _normalize_db_url("postgresql+psycopg://user:pw@host:5432/db")
        assert url == "postgresql+psycopg://user:pw@host:5432/db"

    def test_sqlite_url_is_left_alone(self):
        url = _normalize_db_url("sqlite:///botteu_dev.db")
        assert url == "sqlite:///botteu_dev.db"
