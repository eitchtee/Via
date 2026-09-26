from httpx import AsyncClient

from via.config import Settings
from via.main import create_app


async def test_ui_is_served_with_strict_csp(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert '<script type="module" src="app.js">' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"

    for asset, content_type in [
        ("app.js", "javascript"),
        ("style.css", "css"),
        ("favicon.svg", "svg"),
        ("icon.svg", "svg"),
        ("apple-touch-icon.png", "png"),
        ("icon-192.png", "png"),
        ("icon-512.png", "png"),
        ("maskable-512.png", "png"),
        ("manifest.json", "json"),
    ]:
        r = await client.get(f"/{asset}")
        assert r.status_code == 200 and content_type in r.headers["content-type"]


async def test_api_is_unaffected(client: AsyncClient) -> None:
    r = await client.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    # Swagger UI needs its CDN and inline script, so it gets no CSP.
    r = await client.get("/docs")
    assert r.status_code == 200 and "content-security-policy" not in r.headers


async def test_ui_can_be_disabled(settings: Settings) -> None:
    from httpx import ASGITransport

    settings.web_ui = False
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://via") as c,
    ):
        assert (await c.get("/")).status_code == 404
        assert (await c.get("/v1/info")).json()["features"]["web_ui"] is False


async def test_openapi_documents_contact_targets(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    for path in ("/v1/send", "/v1/pushes/file"):
        [to] = [p for p in spec["paths"][path]["post"]["parameters"] if p["name"] == "to"]
        assert "@username" in to["description"], path
    push_in = spec["components"]["schemas"]["PushIn"]
    assert "@username" in push_in["properties"]["to"]["description"]
    assert push_in["examples"]


async def test_manifest_icons_exist(client: AsyncClient) -> None:
    manifest = (await client.get("/manifest.json")).json()
    for icon in manifest["icons"]:
        assert (await client.get(f"/{icon['src']}")).status_code == 200, icon["src"]
