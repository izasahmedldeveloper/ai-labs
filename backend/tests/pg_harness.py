"""A throw-away real PostgreSQL for the RLS tests.

Order of preference:
  1. TEST_DATABASE_URL (a superuser DSN to an EMPTY, disposable database): used as is, never started or stopped here.
  2. Postgres server binaries found on this machine (PG_BIN, or /usr/lib/postgresql/*/bin): a private cluster is
     initialised in a temp dir (unix socket only, trust auth, no TCP), migrated, and removed at exit.
  3. Otherwise `available()` is False and the tests skip. They are NOT optional for security: run them somewhere
     with Postgres before shipping (see README).
Trust authentication and a private socket are fine for a disposable test cluster and nothing else.
"""
from __future__ import annotations

import atexit
import glob
import os
import pwd
import shutil
import subprocess
import tempfile

_state: dict = {}


def _bin_dir() -> str | None:
    explicit = os.environ.get("PG_BIN")
    candidates = [explicit] if explicit else sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "initdb")) and os.path.exists(os.path.join(c, "postgres")):
            return c
    return None


def _run(args: list[str], *, as_postgres: bool) -> subprocess.CompletedProcess:
    if as_postgres and os.geteuid() == 0:
        args = ["su", "postgres", "-s", "/bin/bash", "-c", " ".join(subprocess.list2cmdline([a]) for a in args)]
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("TEST_DATABASE_URL")) or _bin_dir() is not None


def _dsn(socket_dir: str, user: str, dbname: str) -> str:
    return f"host={socket_dir} port=55432 user={user} dbname={dbname}"


def start() -> dict:
    """Start (once per process) and return {'host': socket dir or None, 'url': base conninfo function}."""
    if _state:
        return _state
    import psycopg
    from studybuddy.db.migrate import bootstrap, migrate

    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        _state.update(kind="external", base=external)
        super_dsn = external
    else:
        bindir = _bin_dir()
        root = tempfile.mkdtemp(prefix="sb_pg_")
        data, sock = os.path.join(root, "data"), os.path.join(root, "sock")
        os.makedirs(sock)
        if os.geteuid() == 0:  # postgres refuses to run as root
            pg_uid = pwd.getpwnam("postgres").pw_uid
            for path in (root, sock):
                os.chmod(path, 0o755)
                os.chown(path, pg_uid, -1)
        out = _run([os.path.join(bindir, "initdb"), "-D", data, "--auth=trust", "-U", "postgres", "--no-sync"], as_postgres=True)
        if out.returncode:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError("initdb failed: " + out.stderr[-400:])
        opts = f"-p 55432 -k {sock} -c listen_addresses='' -c fsync=off -c synchronous_commit=off"
        out = _run([os.path.join(bindir, "pg_ctl"), "-D", data, "-o", opts, "-l", os.path.join(root, "log"), "-w", "start"], as_postgres=True)
        if out.returncode:
            raise RuntimeError("pg_ctl start failed: " + out.stderr[-400:])

        def stop() -> None:
            _run([os.path.join(bindir, "pg_ctl"), "-D", data, "-m", "immediate", "stop"], as_postgres=True)
            shutil.rmtree(root, ignore_errors=True)

        atexit.register(stop)
        _state.update(kind="private", sock=sock)
        super_dsn = _dsn(sock, "postgres", "postgres")

    with psycopg.connect(super_dsn, autocommit=True) as conn:
        bootstrap(conn)
        conn.execute("DROP DATABASE IF EXISTS sb_test")
        conn.execute("CREATE DATABASE sb_test OWNER sb_owner")
    _state["super_dsn"] = _db_dsn(super_dsn, "sb_test", None)
    with psycopg.connect(_db_dsn(super_dsn, "sb_test", "sb_owner")) as conn:
        migrate(conn)
    _state["base"] = super_dsn
    return _state


def _db_dsn(base: str, dbname: str, user: str | None) -> str:
    parts = dict(p.split("=", 1) for p in base.split() if "=" in p) if "://" not in base else None
    if parts is None:  # URL form
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        d = conninfo_to_dict(base)
        d["dbname"] = dbname
        if user:
            d["user"] = user
        return make_conninfo(**d)
    parts["dbname"] = dbname
    if user:
        parts["user"] = user
    return " ".join(f"{k}={v}" for k, v in parts.items())


def dsn_for(role: str) -> str:
    """Connection string for one of the application roles (or 'postgres' for the fixture-loading superuser)."""
    st = start()
    return _db_dsn(st["base"], "sb_test", role if role != "super" else None) if role != "super" else st["super_dsn"]
