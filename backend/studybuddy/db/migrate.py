"""Apply the SQL files in `sql/` in order, once each, and refuse to run if an applied file has changed.

    bootstrap(superuser_conn)      # cluster roles (000), run once by a superuser
    migrate(owner_conn)            # schema + RLS (001, 002, ...), run as sb_owner

Migrations are plain SQL on purpose: the security-critical parts (policies, grants, triggers) should be readable as
written. Each file runs in one transaction. Never run migrations with an application role.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

SQL_DIR = Path(__file__).parent / "sql"
BOOTSTRAP = "000_bootstrap_roles.sql"


class MigrationError(RuntimeError):
    pass


def _files() -> list[Path]:
    return sorted(p for p in SQL_DIR.glob("*.sql") if p.name != BOOTSTRAP)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bootstrap(conn, passwords: dict[str, str] | None = None) -> None:
    """Create the cluster roles (idempotent). `passwords` maps role name to password; omit it when roles authenticate
    by certificate, peer or IAM. Passwords are passed as quoted literals and never logged."""
    from psycopg import sql

    conn.execute((SQL_DIR / BOOTSTRAP).read_text())
    for role, password in (passwords or {}).items():
        if role not in {"sb_owner", "sb_auth", "sb_app", "sb_worker", "sb_eval", "sb_relay"}:
            raise MigrationError(f"unknown role {role!r}")
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    conn.commit()


def migrate(conn) -> list[str]:
    """Apply pending files. Returns the names applied. Raises if a previously applied file was edited."""
    applied: dict[str, str] = {}
    exists = conn.execute("SELECT to_regclass('public.schema_migration') IS NOT NULL").fetchone()[0]
    if exists:
        applied = dict(conn.execute("SELECT name, sha256 FROM schema_migration").fetchall())
    done = []
    for path in _files():
        digest = _digest(path)
        if path.name in applied:
            if applied[path.name] != digest:
                raise MigrationError(f"{path.name} was changed after it was applied; add a new migration instead")
            continue
        conn.execute(path.read_text())
        conn.execute("INSERT INTO schema_migration (name, sha256) VALUES (%s, %s)", (path.name, digest))
        conn.commit()
        done.append(path.name)
    return done
