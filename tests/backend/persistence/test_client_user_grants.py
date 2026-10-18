from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo

from backend.services.client_user_store import ClientUserStore, digest


@pytest.fixture(scope="module")
def desktop_postgres() -> Iterator[ClientUserStore]:
    initdb, postgres = shutil.which("initdb"), shutil.which("postgres")
    if not initdb or not postgres:
        pytest.skip("Local PostgreSQL is required for desktop grant transactions")
    with TemporaryDirectory(prefix="ts-client-grants-") as temporary:
        folder = Path(temporary)
        data, sockets = folder / "data", folder / "socket"
        sockets.mkdir(mode=0o700)
        subprocess.run(
            [
                initdb,
                "-D",
                str(data),
                "-U",
                "client_test",
                "--encoding=UTF8",
                "--no-locale",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
            check=True,
            capture_output=True,
        )
        ready = Event()
        server = subprocess.Popen(
            [postgres, "-D", str(data), "-h", "", "-k", str(sockets), "-F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def output() -> None:
            assert server.stdout
            for line in server.stdout:
                if "database system is ready" in line:
                    ready.set()

        reader = Thread(target=output, daemon=True)
        reader.start()
        store = None
        try:
            assert ready.wait(15)
            dsn = make_conninfo(host=str(sockets), dbname="postgres", user="client_test")
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(
                    "CREATE TABLE app_user (id UUID PRIMARY KEY,email TEXT NOT NULL,"
                    "display_name TEXT,role TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT TRUE)"
                )
                root = Path(__file__).resolve().parents[3]
                c.execute((root / "migrations/019_client_user_authorization.up.sql").read_text())
            store = ClientUserStore(dsn, pool_min_size=0, pool_max_size=2)
            yield store
        finally:
            if store:
                store.close()
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=15)
            reader.join(timeout=5)


def prepared(
    store: ClientUserStore, *, installation: str | None = None, user: str | None = None
) -> tuple[str, str, str]:
    uid = user or str(uuid4())
    install = installation or str(uuid4())
    device, code = str(uuid4()), str(uuid4())
    with store._connection() as c:
        c.execute(
            "INSERT INTO app_user(id,email,role) VALUES(%s,%s,'owner') ON CONFLICT DO NOTHING",
            (uid, uid + "@example.com"),
        )
    store.create_device(
        uid,
        uuid4() if not installation else __import__("uuid").UUID(install),
        "import",
        device,
        code,
    )
    row = store.device(code, uid)
    store.decide(
        code,
        uid,
        True,
        {"application_id": "app", "subscription_id": "sub", "gateway_id": "g"},
        "password",
        datetime.now(UTC),
    )
    gid = uuid4()
    access, refresh = "a" + str(uuid4()), "r" + str(uuid4())
    assert store.issue(row["id"], access, refresh, b"encrypted-result", gid)
    return access, refresh, uid


def test_device_is_user_bound_and_only_approved_once(desktop_postgres: ClientUserStore) -> None:
    store = desktop_postgres
    uid = str(uuid4())
    with store._connection() as c:
        c.execute(
            "INSERT INTO app_user(id,email,role) VALUES(%s,'bound@example.com','member')", (uid,)
        )
    store.create_device(uid, uuid4(), "login", "secret-device", "ABCD-EFGH")
    with pytest.raises(HTTPException) as denied:
        store.device("ABCD-EFGH", str(uuid4()))
    assert denied.value.status_code == 404
    assert store.poll("secret-device")["state"] == "pending"
    with pytest.raises(HTTPException, match="slow_down"):
        store.poll("secret-device")
    store.decide("ABCD-EFGH", uid, True, None, "password", datetime.now(UTC))
    with pytest.raises(HTTPException):
        store.decide("ABCD-EFGH", uid, True, None, "password", datetime.now(UTC))
    with store._connection() as c:
        raw = c.execute(
            "SELECT * FROM client_device_authorization WHERE user_id=%s", (uid,)
        ).fetchone()
        assert raw["device_digest"] == digest("secret-device")
        assert "secret-device" not in json.dumps(raw, default=str)


def test_reauthentication_requires_a_new_platform_session(
    desktop_postgres: ClientUserStore,
) -> None:
    store = desktop_postgres
    uid = str(uuid4())
    with store._connection() as c:
        c.execute(
            "INSERT INTO app_user(id,email,role) VALUES(%s,'reauth@example.com','owner')", (uid,)
        )
    store.create_device(uid, uuid4(), "reauth", "reauth-device", "REAL-2222")
    with pytest.raises(HTTPException):
        store.decide(
            "REAL-2222", uid, True, None, "password", datetime.now(UTC) - timedelta(days=1)
        )
    store.decide("REAL-2222", uid, True, None, "password", datetime.now(UTC))


def test_refresh_is_idempotent_and_replay_revokes(desktop_postgres: ClientUserStore) -> None:
    store = desktop_postgres
    access, refresh, _ = prepared(store)
    row = store.grant(access)
    result = store.rotate(refresh, "request-1", "next-access", "next-refresh", b"result")
    assert result == b"result"
    assert store.rotate(refresh, "request-1", "ignored", "ignored", b"ignored") == b"result"
    with pytest.raises(HTTPException):
        store.grant(access)
    assert store.grant("next-access")["id"] == row["id"]
    assert store.rotate(refresh, "another-request", "a2", "r2", b"other") is None
    with pytest.raises(HTTPException):
        store.grant("next-access")


def test_activation_conflicts_and_other_installations_survive(
    desktop_postgres: ClientUserStore,
) -> None:
    store = desktop_postgres
    installation = str(uuid4())
    first, _, uid = prepared(store, installation=installation)
    second, _, _ = prepared(store, installation=installation, user=uid)
    other, _, _ = prepared(store, user=uid)
    first_row = store.grant(first)
    assert store.activate(first_row["id"])["activated"]
    assert store.activate(first_row["id"])["activated"]
    with pytest.raises(HTTPException):
        store.grant(second)
    assert store.grant(other)["user_id"] == __import__("uuid").UUID(uid)


def test_disabled_user_and_image_only_capability(desktop_postgres: ClientUserStore) -> None:
    store = desktop_postgres
    access, _, uid = prepared(store)
    row = store.activate(store.grant(access)["id"])
    store.image_key(row["id"], "image-only")
    assert store.grant("image-only", image=True)["id"] == row["id"]
    with pytest.raises(HTTPException):
        store.grant("image-only")
    with store._connection() as c:
        c.execute("UPDATE app_user SET enabled=FALSE WHERE id=%s", (uid,))
    with pytest.raises(HTTPException):
        store.grant(access)
    with pytest.raises(HTTPException):
        store.grant("image-only", image=True)
