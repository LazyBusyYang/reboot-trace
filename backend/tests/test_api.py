from reboot_trace.api import create_app
import asyncio
import httpx
from dataclasses import replace


def test_openapi_contains_required_contract(settings):
    schema=create_app(settings).openapi()
    paths=schema["paths"]
    for path in (
        "/health/live", "/health/ready", "/api/v1/status", "/api/v1/latest",
        "/api/v1/lifecycles", "/api/v1/lifecycles/{boot_id}",
        "/api/v1/lifecycles/{boot_id}/snapshots",
        "/api/v1/lifecycles/{boot_id}/final", "/api/v1/lifecycles/{boot_id}/events",
        "/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}/processes",
        "/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}/users",
        "/api/v1/lifecycles/{boot_id}/series", "/api/v1/lifecycles/{boot_id}/compare",
        "/api/v1/lifecycles/{boot_id}/processes/{pid}",
    ):
        assert path in paths
    assert paths["/api/v1/status"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("StatusResponse")
    assert "SystemSample" in schema["components"]["schemas"]
    for path in (
        "/api/v1/lifecycles/{boot_id}/final",
        "/api/v1/lifecycles/{boot_id}/events",
        "/api/v1/lifecycles/{boot_id}/series",
        "/api/v1/lifecycles/{boot_id}/compare",
    ):
        assert "$ref" in paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_http_middleware_uses_read_connection_and_request_id(settings,fake_process):
    async def run():
        app=create_app(replace(settings,service_token="s"*32))
        async with app.router.lifespan_context(app):
            transport=httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,base_url="http://test") as client:
                response=await client.get("/api/v1/status",headers={"X-Request-ID":"acceptance-request"})
                assert response.status_code == 200
                assert response.headers["X-Request-ID"] == "acceptance-request"
                assert response.headers["X-Reboot-Trace-Service-Token"] == "s"*32
                assert response.json()["host_id"] == "host-test"
                allowed=await client.options("/api/v1/latest",headers={"Origin":"https://frontend.example","Access-Control-Request-Method":"GET"})
                assert allowed.headers["access-control-allow-origin"] == "https://frontend.example"
                assert allowed.headers.get("access-control-allow-credentials") != "true"
                denied=await client.options("/api/v1/latest",headers={"Origin":"https://evil.example","Access-Control-Request-Method":"GET"})
                assert "access-control-allow-origin" not in denied.headers
                lifecycle=(await client.get("/api/v1/lifecycles")).json()["items"][0]
                snapshots=(await client.get(f"/api/v1/lifecycles/{lifecycle['boot_id']}/snapshots")).json()["items"]
                assert snapshots and all(item["boot_id"] == lifecycle["boot_id"] and "lifecycle_id" not in item for item in snapshots)
                events=(await client.get(f"/api/v1/lifecycles/{lifecycle['boot_id']}/events")).json()["items"]
                assert events and all(item["boot_id"] == lifecycle["boot_id"] and "lifecycle_id" not in item for item in events)
                final=await client.get(f"/api/v1/lifecycles/{lifecycle['boot_id']}/final")
                assert final.status_code == 200
                assert "top" in final.json()["snapshots"][0]
    asyncio.run(run())
