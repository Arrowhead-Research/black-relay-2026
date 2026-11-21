"""AtakMissionPackages against a stand-in for TAK Server's mission API (a real aiohttp server on localhost)."""

import asyncio
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pytak_gateway.mission_package import AtakMissionPackages, MissionPackageError, MissionPackageExists

PASSWORD = "s3cret-mission-pw"


def run_against(handler, action):
    """Run `action(client)` against a server whose every route is `handler`; return the requests it saw."""
    seen = []

    async def recording(request: web.Request) -> web.Response:
        seen.append((request.method, request.raw_path.split("?")[0], dict(request.query)))
        return await handler(request)

    async def main():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", recording)
        async with TestServer(app) as server:
            client = AtakMissionPackages(str(server.make_url("")))
            try:
                await action(client)
            finally:
                await client.close()

    asyncio.run(main())
    return seen


def answering(status: int):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=status, text="body")

    return handler


def test_create_puts_the_mission_with_its_password_and_owner():
    seen = run_against(answering(201), lambda c: c.create("sh1-mission", PASSWORD))
    assert seen == [("PUT", "/Marti/api/missions/sh1-mission", {"password": PASSWORD, "creatorUid": "pytak-gateway"})]


def test_create_accepts_200():
    run_against(answering(200), lambda c: c.create("m", PASSWORD))


def test_create_conflict_is_mission_package_exists():
    with pytest.raises(MissionPackageExists):
        run_against(answering(409), lambda c: c.create("m", PASSWORD))


@pytest.mark.parametrize("status", [401, 403])
def test_create_refused_by_tak(status):
    with pytest.raises(MissionPackageError, match="did not allow"):
        run_against(answering(status), lambda c: c.create("m", PASSWORD))


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_create_other_failures(status):
    with pytest.raises(MissionPackageError) as e:
        run_against(answering(status), lambda c: c.create("m", PASSWORD))
    assert not isinstance(e.value, MissionPackageExists)


def test_remove_deletes_the_mission():
    seen = run_against(answering(200), lambda c: c.remove("sh1-mission", PASSWORD))
    expected = {"password": PASSWORD, "creatorUid": "pytak-gateway"}
    assert seen == [("DELETE", "/Marti/api/missions/sh1-mission", expected)]


def test_remove_accepts_204():
    run_against(answering(204), lambda c: c.remove("m", PASSWORD))


def test_remove_of_a_mission_already_gone_is_removed():
    run_against(answering(404), lambda c: c.remove("m", PASSWORD))


@pytest.mark.parametrize("status", [401, 403, 500])
def test_remove_failures(status):
    with pytest.raises(MissionPackageError):
        run_against(answering(status), lambda c: c.remove("m", PASSWORD))


def test_the_name_is_percent_encoded():
    seen = run_against(answering(201), lambda c: c.create("a b/c", PASSWORD))
    assert seen[0][1] == "/Marti/api/missions/a%20b%2Fc"


def test_no_answer_is_an_error_without_url_or_password():
    async def main():
        client = AtakMissionPackages("https://127.0.0.1:1")  # nothing listens
        try:
            await client.create("m", PASSWORD)
        finally:
            await client.close()

    with pytest.raises(MissionPackageError) as e:
        asyncio.run(main())
    assert PASSWORD not in str(e.value) and "127.0.0.1" not in str(e.value)


def test_a_slow_answer_times_out():
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.Response()

    async def main():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", slow)
        async with TestServer(app) as server:
            client = AtakMissionPackages(str(server.make_url("")), timeout=0.1)
            try:
                await client.create("m", PASSWORD)
            finally:
                await client.close()

    with pytest.raises(MissionPackageError, match="did not answer"):
        asyncio.run(main())


def test_without_a_url_every_call_refuses():
    async def main():
        client = AtakMissionPackages(None)
        with pytest.raises(MissionPackageError, match="TAK_API_URL"):
            await client.create("m", PASSWORD)
        with pytest.raises(MissionPackageError, match="TAK_API_URL"):
            await client.remove("m", PASSWORD)

    asyncio.run(main())


def test_from_env_without_a_url_refuses(monkeypatch):
    monkeypatch.delenv("TAK_API_URL", raising=False)
    assert AtakMissionPackages.from_env().base_url is None


def test_from_env_with_an_unreadable_certificate_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("TAK_API_URL", "https://takserver:8443")
    monkeypatch.setenv("TAK_API_CERT", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("TAK_API_KEY", str(tmp_path / "missing.key"))
    with pytest.raises(OSError):
        AtakMissionPackages.from_env()


def test_the_password_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    run_against(answering(404), lambda c: c.remove("m", PASSWORD))
    with pytest.raises(MissionPackageError):
        run_against(answering(500), lambda c: c.create("m", PASSWORD))
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("pytak_gateway"))
    assert ours and PASSWORD not in ours  # the stand-in server's own access log is not ours


def test_remove_items_deletes_each_uid_from_the_contents():
    seen = run_against(answering(200), lambda c: c.remove_items("sh1-mission", ["a", "b"]))
    assert seen == [
        ("DELETE", "/Marti/api/missions/sh1-mission/contents", {"uid": "a", "creatorUid": "pytak-gateway"}),
        ("DELETE", "/Marti/api/missions/sh1-mission/contents", {"uid": "b", "creatorUid": "pytak-gateway"}),
    ]


def test_remove_items_of_a_mission_that_is_gone_stops_without_error():
    seen = run_against(answering(404), lambda c: c.remove_items("m", ["a", "b"]))
    assert len(seen) == 1


@pytest.mark.parametrize("status", [403, 500])
def test_remove_items_failures(status):
    with pytest.raises(MissionPackageError, match="take an item out of"):
        run_against(answering(status), lambda c: c.remove_items("m", ["a"]))
