from __future__ import annotations

import asyncio
import socket
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from reboot_trace.api import create_app
from reboot_trace.collector import ProcCollector
from reboot_trace.database import Repository
from test_database import sample
from reboot_trace.schemas import StatusResponse,LatestResponse


def endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route,"path",None)==path)


def request(app) -> Request:
    return Request({"type":"http","method":"GET","path":"/","headers":[],"query_string":b"","app":app})


def test_status_and_latest_behavior(settings,fake_process):
    async def run():
        app=create_app(settings)
        collector=ProcCollector(settings)
        repo=Repository(settings,socket.gethostname(),"test")
        repo.start_lifecycle(collector.boot_id(),collector.container_started_at_ms(),collector.current_identity.as_dict())
        repo.write_snapshot(collector.collect())
        app.state.repo=repo
        app.state.collector=collector
        try:
            status=await endpoint(app,"/api/v1/status")(request(app))
            latest=await endpoint(app,"/api/v1/latest")(request(app))
            assert status["host_id"] == "host-test"
            assert status["lifecycle"]["termination"] == "active"
            assert status["identity"]["detection_method"] == "instance_marker"
            assert status["identity"]["identity_scope"] == "local_container"
            assert status["identity"]["marker_storage"] == "container_ephemeral_rootfs"
            assert status["identity"]["namespace_state"] == "supported"
            assert "detection_method" not in status["lifecycle"]
            assert status["storage"]["mountpoint"].endswith("/data")
            assert status["storage"]["persistence_capability"] == "operator_verification_required"
            assert latest["system"]["process_count"] == 2
            assert latest["system"]["process_sampled_count"] == 2
            assert "supersecret" not in str(latest)
            StatusResponse.model_validate(status);LatestResponse.model_validate(latest)
        finally:
            repo.close()
    asyncio.run(run())


def test_evidence_query_endpoints(settings):
    async def run():
        app=create_app(settings);repo=Repository(settings,"host","test");repo.start_lifecycle("boot",1)
        first=repo.write_snapshot(sample("boot"));second=repo.write_snapshot(sample("boot"));app.state.repo=repo
        req=request(app)
        try:
            process=await endpoint(app,"/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}/processes")(req,"boot",second,"cpu",None,50,None)
            assert process["items"][0]["cmdline_redacted"] == "python x"
            final=await endpoint(app,"/api/v1/lifecycles/{boot_id}/final")(req,"boot",1)
            assert final["snapshots"][0]["top"]["cpu"][0]["pid"] == 1
            users=await endpoint(app,"/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}/users")(req,"boot",second)
            assert users["items"][0]["uid"] == 1000
            series=await endpoint(app,"/api/v1/lifecycles/{boot_id}/series")(req,"boot","host_cpu_percent",None,None,"raw")
            assert len(series["series"][0]["points"]) == 2
            compared=await endpoint(app,"/api/v1/lifecycles/{boot_id}/compare")(req,"boot",first,second)
            assert compared["left_snapshot_id"] == first and len(compared["users"]) == 1
            history=await endpoint(app,"/api/v1/lifecycles/{boot_id}/processes/{pid}")(req,"boot",1,10,None,None,100)
            assert len(history["items"]) == 2
        finally: repo.close()
    asyncio.run(run())


def test_lifecycle_and_process_history_cursor_pagination(settings):
    async def run():
        app=create_app(settings);repo=Repository(settings,"host","test")
        repo.start_lifecycle("boot-a",1)
        for _ in range(3): repo.write_snapshot(sample("boot-a"))
        repo.start_lifecycle("boot-b",2);repo.start_lifecycle("boot-c",3)
        app.state.repo=repo;req=request(app)
        try:
            list_endpoint=endpoint(app,"/api/v1/lifecycles")
            first=await list_endpoint(req,None,None,None,2,None)
            second=await list_endpoint(req,None,None,None,2,first["next_cursor"])
            assert [item["boot_id"] for item in first["items"]] == ["boot-c","boot-b"]
            assert [item["boot_id"] for item in second["items"]] == ["boot-a"]
            history_endpoint=endpoint(app,"/api/v1/lifecycles/{boot_id}/processes/{pid}")
            history1=await history_endpoint(req,"boot-a",1,10,None,None,2,None)
            history2=await history_endpoint(req,"boot-a",1,10,None,None,2,history1["next_cursor"])
            assert len(history1["items"]) == 2 and len(history2["items"]) == 1
            assert {item["snapshot_id"] for item in history1["items"]}.isdisjoint({item["snapshot_id"] for item in history2["items"]})
        finally: repo.close()
    asyncio.run(run())


def test_legacy_boot_route_is_rejected_when_multiple_container_instances_match(settings):
    async def run():
        app=create_app(settings);repo=Repository(settings,"host","test")
        base={"pid1_start_ticks":100,"pid_namespace_inode":1,"cgroup_hash":"a","fingerprint":"a"}
        repo.start_lifecycle("shared-kernel",1,base|{"container_instance_id":"instance-a","preferred_key":"instance-a"})
        repo.start_lifecycle("shared-kernel",2,base|{"container_instance_id":"instance-b","preferred_key":"instance-b"})
        app.state.repo=repo
        try:
            lifecycle_endpoint=endpoint(app,"/api/v1/lifecycles/{boot_id}")
            with pytest.raises(HTTPException) as raised:
                await lifecycle_endpoint(request(app),"shared-kernel")
            assert raised.value.status_code==409 and raised.value.detail=="AMBIGUOUS_LIFECYCLE"
            exact=await lifecycle_endpoint(request(app),"instance-b")
            assert exact["lifecycle_key"]=="instance-b"
        finally:repo.close()
    asyncio.run(run())
