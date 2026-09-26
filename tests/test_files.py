import base64
import hashlib
import os
from datetime import timedelta

from httpx import AsyncClient

from via import timeutil
from via.config import Settings
from via.crypto import CHUNK_SIZE
from via.services import janitor

from .conftest import Api, Device, auth, expect

DATA = os.urandom(3 * CHUNK_SIZE + 1234)


async def _setup(api: Api) -> tuple[str, Device, Device]:
    session = await api.user()
    return session, await api.device(session, "phone"), await api.device(session, "laptop")


async def _upload(
    client: AsyncClient, sender: Device, data: bytes = DATA, status: int = 201, **params: str
) -> dict:  # type: ignore[type-arg]
    params = {"filename": "report é.pdf", **params}
    r = await client.post(
        "/v1/pushes/file",
        params=params,
        content=data,
        headers={**sender.headers, "Content-Type": "application/pdf"},
    )
    return expect(r, status)  # type: ignore[no-any-return]


async def test_file_lifecycle(api: Api, client: AsyncClient) -> None:
    session, phone, laptop = await _setup(api)
    push = await _upload(client, phone, title="Q3")

    blob = api.ctx.blobs.path(push["id"])
    assert blob.is_file()
    assert DATA[:1000] not in blob.read_bytes()  # encrypted at rest

    [item] = await api.inbox(laptop)
    assert item["kind"] == "file"
    assert item["title"] == "Q3"
    assert item["file"] == {
        "name": "report é.pdf",
        "mime": "application/pdf",
        "size": len(DATA),
        "sha256": hashlib.sha256(DATA).hexdigest(),
        "available": True,
    }
    account = expect(await client.get("/v1/account", headers=auth(session)), 200)
    assert account["usage"]["pending_bytes"] == len(DATA)

    r = await client.get(f"/v1/inbox/{push['id']}/file", headers=laptop.headers)
    assert r.status_code == 200
    assert r.content == DATA
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-via-sha256"] == hashlib.sha256(DATA).hexdigest()
    digest = base64.b64encode(hashlib.sha256(DATA).digest()).decode()
    assert r.headers["repr-digest"] == f"sha-256=:{digest}:"
    assert "filename*=UTF-8''report%20%C3%A9.pdf" in r.headers["content-disposition"]

    await api.ack(laptop, push["id"])
    assert not blob.exists()
    expect(await client.get(f"/v1/inbox/{push['id']}/file", headers=laptop.headers), 404)
    account = expect(await client.get("/v1/account", headers=auth(session)), 200)
    assert account["usage"]["pending_bytes"] == 0


async def test_range_requests(api: Api, client: AsyncClient) -> None:
    _, phone, laptop = await _setup(api)
    push = await _upload(client, phone)
    url = f"/v1/inbox/{push['id']}/file"
    size = len(DATA)

    for header, (start, end) in {
        "bytes=0-99": (0, 99),
        f"bytes={CHUNK_SIZE - 5}-{CHUNK_SIZE + 5}": (CHUNK_SIZE - 5, CHUNK_SIZE + 5),
        "bytes=100000-": (100000, size - 1),
        "bytes=-10": (size - 10, size - 1),
        f"bytes=5-{size + 100}": (5, size - 1),
    }.items():
        r = await client.get(url, headers={**laptop.headers, "Range": header})
        assert r.status_code == 206, header
        assert r.content == DATA[start : end + 1], header
        assert r.headers["content-range"] == f"bytes {start}-{end}/{size}"

    r = await client.get(url, headers={**laptop.headers, "Range": f"bytes={size}-"})
    assert expect(r, 416)["error"]["code"] == "range_not_satisfiable"


async def test_empty_file(api: Api, client: AsyncClient) -> None:
    _, phone, laptop = await _setup(api)
    push = await _upload(client, phone, data=b"")
    r = await client.get(f"/v1/inbox/{push['id']}/file", headers=laptop.headers)
    assert r.status_code == 200 and r.content == b""


async def test_size_limit_and_quota(api: Api, client: AsyncClient, settings: Settings) -> None:
    _, phone, _ = await _setup(api)
    settings.max_file_size = 1000
    r = await _upload(client, phone, data=b"x" * 1001, status=413)
    assert r["error"]["code"] == "file_too_large"

    settings.user_quota = 1500
    await _upload(client, phone, data=b"x" * 1000)
    r = await _upload(client, phone, data=b"x" * 600, status=413)
    assert r["error"]["code"] == "quota_exceeded"
    await _upload(client, phone, data=b"x" * 500)
    # Nothing half-written is left behind.
    assert not [p for _, p, partial, _ in api.ctx.blobs.scan() if partial]


async def test_orphaned_blobs_are_cleaned(api: Api, client: AsyncClient) -> None:
    _, phone, _ = await _setup(api)
    push = await _upload(client, phone)
    orphan = api.ctx.blobs.path("01ORPHAN0000000000000000XX")
    orphan.parent.mkdir(exist_ok=True)
    orphan.write_bytes(b"junk")
    old = (timeutil.utcnow() - timedelta(hours=1)).timestamp()
    os.utime(orphan, (old, old))
    os.utime(api.ctx.blobs.path(push["id"]), (old, old))

    await janitor.run_once(api.ctx)
    assert not orphan.exists()
    assert api.ctx.blobs.path(push["id"]).exists()


async def test_send_shortcut(api: Api, client: AsyncClient) -> None:
    session, phone, laptop = await _setup(api)
    app_token = expect(
        await client.post("/v1/app-tokens", json={"name": "curl"}, headers=auth(session)), 201
    )["token"]
    headers = auth(app_token)

    expect(
        await client.post("/v1/send", content=b"https://example.com/a?b=c", headers=headers), 201
    )
    expect(await client.post("/v1/send?title=Hi", content=b"buy milk\n", headers=headers), 201)
    expect(await client.put("/v1/send/notes.txt", content=b"file body", headers=headers), 201)
    r = await client.post("/v1/send", content=b"  ", headers=headers)
    assert expect(r, 422)["error"]["code"] == "empty_body"
    r = await client.post("/v1/send", content=b"\xff\xfe", headers=headers)
    assert expect(r, 400)["error"]["code"] == "not_text"

    items = await api.inbox(laptop)
    assert [(i["kind"], i["url"], i["body"], i["title"]) for i in items[:2]] == [
        ("link", "https://example.com/a?b=c", None, None),
        ("note", None, "buy milk", "Hi"),
    ]
    assert items[2]["kind"] == "file" and items[2]["file"]["name"] == "notes.txt"
    r = await client.get(f"/v1/inbox/{items[2]['id']}/file", headers=laptop.headers)
    assert r.content == b"file body"
    # The phone is also a target when an app token sends to "all".
    assert len(await api.inbox(phone)) == 3
