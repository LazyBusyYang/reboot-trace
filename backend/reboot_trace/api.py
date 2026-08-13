from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .collector import ProcCollector
from .config import Settings
from .database import Repository, SCHEMA_VERSION
from .schemas import (CompareResponse, EventPage, FinalResponse, FullSnapshotResponse,
                      LatestResponse, LifecyclePage, LifecycleResponse,
                      ProcessHistoryResponse, ProcessPage, PublicConfigResponse,
                      SeriesResponse, SnapshotPage, StatusResponse, UserPage)


def _response(repo: Repository, payload: dict[str, Any]) -> dict[str, Any]:
    return {"api_version":"1","schema_version":SCHEMA_VERSION,"host_id":repo.host_id,**payload}


def _cursor(value: int, scope: str) -> str:
    payload=json.dumps({"v":value,"s":hashlib.sha256(scope.encode()).hexdigest()[:12]},separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str | None, scope: str) -> int | None:
    if value is None: return None
    try:
        payload=json.loads(base64.urlsafe_b64decode(value+"="*(-len(value)%4)))
        if payload["s"] != hashlib.sha256(scope.encode()).hexdigest()[:12]: raise ValueError
        return int(payload["v"])
    except (ValueError,KeyError,TypeError,json.JSONDecodeError):
        raise HTTPException(422,"INVALID_CURSOR")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import socket
        collector = ProcCollector(settings)
        repo = Repository(settings, socket.gethostname(), __version__)
        initial_identity = collector.current_identity
        repo.start_lifecycle(initial_identity.kernel_boot_id, collector.container_started_at_ms(initial_identity), initial_identity.as_dict())
        app.state.collector, app.state.repo = collector, repo
        app.state.query_semaphore = asyncio.Semaphore(8)
        stop = asyncio.Event()
        app.state.stop = stop
        reclaim_task: asyncio.Task[None] | None = None
        async def loop() -> None:
            nonlocal reclaim_task
            next_at = time.monotonic()
            next_wall_ms = time.time() * 1000
            while not stop.is_set():
                scheduled = int(next_wall_ms)
                try:
                    data = collector.collect(scheduled)
                    if repo.identity_changed(data.identity):
                        repo.start_lifecycle(data.boot_id, collector.container_started_at_ms(collector.current_identity), data.identity, collector_started_event=False)
                    data.lifecycle_key = repo.status()["lifecycle_key"]
                    repo.write_snapshot(data)
                    if repo.reclaim_requested and (reclaim_task is None or reclaim_task.done()):
                        reclaim_task = asyncio.create_task(asyncio.to_thread(repo.reclaim))
                except Exception as exc:
                    app.state.last_error = f"{type(exc).__name__}: {exc}"
                    repo.record_event("collector_error",{"error_type":type(exc).__name__})
                next_at += settings.sample_interval_ms/1000
                next_wall_ms += settings.sample_interval_ms
                try: await asyncio.wait_for(stop.wait(), max(0,next_at-time.monotonic()))
                except TimeoutError: pass
        task=asyncio.create_task(loop())
        yield
        stop.set()
        await task
        if reclaim_task is not None:
            await reclaim_task
        repo.close()

    app=FastAPI(title="Reboot Trace API",version=__version__,lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware,allow_origins=list(settings.cors_origins),allow_credentials=False,allow_methods=["GET","OPTIONS"],allow_headers=["Content-Type","X-Request-ID"])

    @app.middleware("http")
    async def database_request_context(request: Request, call_next):
        repository=getattr(request.app.state,"repo",None)
        request_id=request.headers.get("x-request-id") or hashlib.sha256(f"{time.time_ns()}:{id(request)}".encode()).hexdigest()[:24]
        request.state.request_id=request_id
        if repository is None or request.url.path == "/health/live":
            response=await call_next(request);response.headers["X-Request-ID"]=request_id;return response
        long_query = any(token in request.url.path for token in ("/series", "/compare", "/processes/"))
        if long_query and repository.managed_bytes() >= int(settings.storage_limit_bytes * 0.85):
            response=JSONResponse(status_code=503,content={"code":"BUSY","message":"存储治理期间暂不接受长查询","details":{},"request_id":request_id,"host_id":repository.host_id})
            response.headers["X-Request-ID"]=request_id
            return response
        semaphore=getattr(request.app.state,"query_semaphore",None) or asyncio.Semaphore(8)
        async with semaphore:
            connection=repository.read_connection()
            request.state.db=connection
            try:
                response=await call_next(request)
            except sqlite3.OperationalError as exc:
                code="QUERY_TOO_LARGE" if "interrupted" in str(exc).lower() else "BUSY"
                response=JSONResponse(status_code=503,content={"code":code,"message":"查询超时或数据库繁忙","details":{},"request_id":request_id,"host_id":repository.host_id})
            finally:
                connection.close()
        response.headers["X-Request-ID"]=request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = exc.detail if isinstance(exc.detail,str) and exc.detail.isupper() else "INVALID_ARGUMENT"
        repository=getattr(request.app.state,"repo",None)
        content={"code":code,"message":str(exc.detail),"details":{},"request_id":getattr(request.state,"request_id",None)}
        if repository is not None: content["host_id"]=repository.host_id
        return JSONResponse(status_code=exc.status_code,content=content)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request:Request,exc:RequestValidationError):
        details=[{"location":".".join(str(x) for x in e["loc"]),"type":e["type"]} for e in exc.errors()]
        repository=getattr(request.app.state,"repo",None)
        content={"code":"INVALID_ARGUMENT","message":"请求参数无效","details":details,"request_id":getattr(request.state,"request_id",None)}
        if repository is not None: content["host_id"]=repository.host_id
        return JSONResponse(status_code=422,content=content)

    @app.exception_handler(Exception)
    async def internal_error(request:Request,exc:Exception):
        repository=getattr(request.app.state,"repo",None)
        content={"code":"INTERNAL_ERROR","message":"服务器内部错误","details":{"error_type":type(exc).__name__},"request_id":getattr(request.state,"request_id",None)}
        if repository is not None: content["host_id"]=repository.host_id
        return JSONResponse(status_code=500,content=content)

    def repo(request: Request) -> Repository: return request.app.state.repo
    def db(request: Request) -> sqlite3.Connection: return getattr(request.state,"db",repo(request).db)

    @app.get("/health/live")
    async def live(): return {"status":"ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        db(request).execute("SELECT 1").fetchone(); return {"status":"ready"}

    @app.get("/api/v1/status",response_model=StatusResponse)
    async def status(request: Request):
        r=repo(request); connection=db(request); value=r.status(connection); last=value.pop("last")
        lifecycle=value.pop("lifecycle")
        latest_row=r.latest(connection); capabilities=(latest_row or {}).get("system",{}).get("capabilities",{})
        capabilities["host_usernames"]={"state":"supported" if settings.host_passwd and settings.host_passwd.exists() else "unsupported","reason":None}
        active_collector=getattr(request.app.state,"collector",None)
        marker_state=active_collector.current_identity.marker_state if active_collector else ("supported" if lifecycle["container_instance_id"] else "unsupported")
        marker_reason=active_collector.current_identity.marker_reason if active_collector else None
        capabilities["container_identity"]={"state":marker_state,"reason":marker_reason}
        persisted_identity={key:lifecycle[key] for key in ("container_instance_id","pid1_start_ticks","pid_namespace_inode","cgroup_hash","detection_method","detection_confidence")}
        runtime_identity=active_collector.current_identity.as_dict() if active_collector else {}
        return _response(r,{**value,"server_time_ms":int(time.time()*1000),"backend_version":__version__,"identity":{**persisted_identity,**{key:runtime_identity.get(key) for key in ("identity_scope","marker_storage","marker_mountpoint","marker_fs_type","namespace_state","namespace_reason")}},"lifecycle":{"termination":lifecycle["termination"],"started_at_ms":lifecycle["started_at_ms"]},"last_persisted_at_ms":last["captured_at_ms"] if last else None,"collector":{"sample_interval_ms":settings.sample_interval_ms,"last_duration_ms":last["duration_ms"] if last else None,"schedule_delay_ms":last["captured_at_ms"]-last["scheduled_at_ms"] if last else None,"last_error":getattr(request.app.state,"last_error",None)},"capabilities":capabilities})

    @app.get("/api/v1/latest",response_model=LatestResponse)
    async def latest(request: Request):
        r=repo(request); row=r.latest(db(request))
        if not row: raise HTTPException(404,"NOT_FOUND")
        status_value=r.status()
        return _response(r,{"boot_id":status_value["boot_id"],"lifecycle_key":status_value["lifecycle_key"],"snapshot_id":row["id"],"captured_at_ms":row["captured_at_ms"],"detail_level":row["detail_level"],"system":row["system"],"collector":{"duration_ms":row["duration_ms"],"schedule_delay_ms":row["captured_at_ms"]-row["scheduled_at_ms"]},"storage":status_value["storage"]})

    @app.get("/api/v1/config/public",response_model=PublicConfigResponse)
    async def public_config(request: Request): return _response(repo(request),{"sample_interval_ms":settings.sample_interval_ms,"process_top_n":settings.process_top_n,"storage_limit_bytes":settings.storage_limit_bytes,"cmdline_max_bytes":settings.cmdline_max_bytes})

    @app.get("/api/v1/lifecycles",response_model=LifecyclePage)
    async def lifecycles(request: Request, termination: str|None=None, from_ms:int|None=None, to_ms:int|None=None, limit:int=Query(100,ge=1,le=1000), cursor:str|None=None):
        r=repo(request); connection=db(request); scope=f"lifecycles:{termination}:{from_ms}:{to_ms}"; cursor_id=_decode_cursor(cursor,scope); sql="SELECT * FROM lifecycle WHERE host_id=?"; params:list[Any]=[r.host_id]
        if termination: sql+=" AND termination=?"; params.append(termination)
        if from_ms is not None: sql+=" AND last_seen_at_ms>=?"; params.append(from_ms)
        if to_ms is not None: sql+=" AND started_at_ms<=?"; params.append(to_ms)
        if cursor_id: sql+=" AND id<?"; params.append(cursor_id)
        sql+=" ORDER BY id DESC LIMIT ?"; params.append(limit+1)
        rows=[dict(x) for x in connection.execute(sql,params).fetchall()]; more=len(rows)>limit; rows=rows[:limit]
        for row in rows:
            row["snapshot_count"]=connection.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(row["id"],)).fetchone()[0]
            summary=json.loads(row.get("summary_json") or "{}")
            row["retention_state"]=summary.get("retention_state", "complete" if row["snapshot_count"] else "summary_only")
        return _response(r,{"items":rows,"next_cursor":_cursor(rows[-1]["id"],scope) if more else None})

    def lifecycle_row(r:Repository,connection:sqlite3.Connection,lifecycle_ref:str):
        exact=connection.execute("SELECT * FROM lifecycle WHERE host_id=? AND lifecycle_key=?",(r.host_id,lifecycle_ref)).fetchone()
        if exact:return exact
        rows=connection.execute("SELECT * FROM lifecycle WHERE host_id=? AND boot_id=? ORDER BY id DESC LIMIT 2",(r.host_id,lifecycle_ref)).fetchall()
        if not rows:raise HTTPException(404,"NOT_FOUND")
        if len(rows)>1:raise HTTPException(409,"AMBIGUOUS_LIFECYCLE")
        return rows[0]

    def lifecycle_payload(lc:sqlite3.Row) -> dict[str,str]:
        return {"boot_id":lc["boot_id"],"lifecycle_key":lc["lifecycle_key"]}

    @app.get("/api/v1/lifecycles/{boot_id}",response_model=LifecycleResponse)
    async def lifecycle(request:Request,boot_id:str):
        r=repo(request); connection=db(request); row=dict(lifecycle_row(r,connection,boot_id)); row["snapshot_count"]=connection.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(row["id"],)).fetchone()[0]; summary=json.loads(row.get("summary_json") or "{}"); row["retention_state"]=summary.get("retention_state", "complete" if row["snapshot_count"] else "summary_only"); return _response(r,row)

    @app.get("/api/v1/lifecycles/{boot_id}/snapshots",response_model=SnapshotPage)
    async def snapshots(request:Request,boot_id:str,from_ms:int|None=None,to_ms:int|None=None,detail_level:str|None=None,limit:int=Query(100,ge=1,le=1000),cursor:str|None=None):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id); scope=f"snapshots:{boot_id}:{from_ms}:{to_ms}:{detail_level}";cursor_id=_decode_cursor(cursor,scope);sql="SELECT * FROM snapshot WHERE lifecycle_id=?"; params:list[Any]=[lc["id"]]
        if from_ms is not None: sql+=" AND captured_at_ms>=?"; params.append(from_ms)
        if to_ms is not None: sql+=" AND captured_at_ms<=?"; params.append(to_ms)
        if detail_level is not None: sql+=" AND detail_level=?";params.append(detail_level)
        if cursor_id: sql+=" AND id<?"; params.append(cursor_id)
        sql+=" ORDER BY id DESC LIMIT ?"; params.append(limit+1); rows=[dict(x) for x in connection.execute(sql,params).fetchall()]; more=len(rows)>limit; rows=rows[:limit]
        for row in rows:
            row.pop("lifecycle_id",None); row.update(lifecycle_payload(lc))
        return _response(r,{**lifecycle_payload(lc),"items":rows,"next_cursor":_cursor(rows[-1]["id"],scope) if more else None})

    def full_snapshot(connection:sqlite3.Connection,lc_id:int,snapshot_id:int)->dict[str,Any]:
        row=connection.execute("SELECT s.*,ss.data_json FROM snapshot s JOIN system_sample ss ON ss.snapshot_id=s.id WHERE s.lifecycle_id=? AND s.id=?",(lc_id,snapshot_id)).fetchone()
        if not row: raise HTTPException(404,"NOT_FOUND")
        result={k:row[k] for k in row.keys() if k!="data_json"}; result["system"]=json.loads(row["data_json"])
        users=[json.loads(x[0]) for x in connection.execute("SELECT data_json FROM user_sample WHERE snapshot_id=?",(snapshot_id,))]
        processes=[]
        for p in connection.execute("SELECT * FROM process_sample WHERE snapshot_id=?",(snapshot_id,)):
            item=dict(p); item["truncated"]=bool(item["truncated"]); item["ranks"]={x[0]:{"rank":x[1],"value":x[2]} for x in connection.execute("SELECT dimension,rank,value FROM process_rank WHERE snapshot_id=? AND pid=? AND create_time_ms=?",(snapshot_id,p["pid"],p["create_time_ms"]))}; processes.append(item)
        top={dimension:sorted((p for p in processes if dimension in p["ranks"]),key=lambda p:p["ranks"][dimension]["rank"]) for dimension in ("cpu","rss","swap","read","write")}
        result.update(users=users,processes=processes,top=top); return result

    @app.get("/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}",response_model=FullSnapshotResponse)
    async def snapshot(request:Request,boot_id:str,snapshot_id:int):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id); return _response(r,{**lifecycle_payload(lc),**full_snapshot(connection,lc["id"],snapshot_id)})

    @app.get("/api/v1/lifecycles/{boot_id}/final",response_model=FinalResponse)
    async def final(request:Request,boot_id:str,count:int=Query(1,ge=1,le=10)):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id); ids=[x[0] for x in connection.execute("SELECT id FROM snapshot WHERE lifecycle_id=? AND persistence_state='normal' AND detail_level IN ('full','final') ORDER BY (detail_level='final') DESC,captured_at_ms DESC LIMIT ?",(lc["id"],count))]
        if not ids: raise HTTPException(410,"DATA_REMOVED")
        snapshots_payload=[]
        for snapshot_id in ids:
            item=full_snapshot(connection,lc["id"],snapshot_id); item.pop("processes",None); item.pop("lifecycle_id",None); item.update(lifecycle_payload(lc)); snapshots_payload.append(item)
        following=connection.execute("SELECT identity_first_observed_at_ms FROM lifecycle WHERE host_id=? AND id>? ORDER BY id LIMIT 1",(r.host_id,lc["id"])).fetchone()
        evidence_gap=max(0,following[0]-lc["last_seen_at_ms"]) if following and following[0] else None
        return _response(r,{**lifecycle_payload(lc),"termination":lc["termination"],"last_persisted_at_ms":lc["last_seen_at_ms"],"evidence_gap_ms":evidence_gap,"snapshots":snapshots_payload})

    @app.get("/api/v1/lifecycles/{boot_id}/events",response_model=EventPage)
    async def events(request:Request,boot_id:str,limit:int=Query(100,ge=1,le=1000),cursor:str|None=None):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id);scope=f"events:{boot_id}";cursor_id=_decode_cursor(cursor,scope);rows=[];sql="SELECT * FROM event WHERE lifecycle_id=?";params:list[Any]=[lc["id"]]
        if cursor_id:sql+=" AND id<?";params.append(cursor_id)
        sql+=" ORDER BY id DESC LIMIT ?";params.append(limit+1)
        raw=connection.execute(sql,params).fetchall();more=len(raw)>limit
        for x in raw[:limit]:
            item=dict(x); item["details"]=json.loads(item.pop("details_json")); rows.append(item)
        for row in rows:
            row.pop("lifecycle_id",None); row.update(lifecycle_payload(lc))
        return _response(r,{**lifecycle_payload(lc),"items":rows,"next_cursor":_cursor(rows[-1]["id"],scope) if more else None})

    @app.get("/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}/processes",response_model=ProcessPage)
    async def processes(request:Request,boot_id:str,snapshot_id:int,dimension:str="cpu",user:int|None=None,limit:int=Query(50,ge=1,le=1000),cursor:str|None=None):
        if dimension not in {"cpu","rss","swap","read","write"}: raise HTTPException(422,"INVALID_ARGUMENT")
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id);scope=f"processes:{boot_id}:{snapshot_id}:{dimension}:{user}";cursor_rank=_decode_cursor(cursor,scope)
        exists=connection.execute("SELECT 1 FROM snapshot WHERE lifecycle_id=? AND id=?",(lc["id"],snapshot_id)).fetchone()
        if not exists: raise HTTPException(404,"NOT_FOUND")
        sql="SELECT p.*,r.rank,r.value,r.dimension FROM process_rank r JOIN process_sample p USING(snapshot_id,pid,create_time_ms) WHERE r.snapshot_id=? AND r.dimension=?"; params:list[Any]=[snapshot_id,dimension]
        if user is not None: sql+=" AND p.uid=?"; params.append(user)
        if cursor_rank is not None: sql+=" AND r.rank>?"; params.append(cursor_rank)
        sql+=" ORDER BY r.rank LIMIT ?"; params.append(limit+1); rows=[dict(x) for x in connection.execute(sql,params)]; more=len(rows)>limit; rows=rows[:limit]
        for row in rows: row["truncated"]=bool(row["truncated"])
        return _response(r,{**lifecycle_payload(lc),"snapshot_id":snapshot_id,"dimension":dimension,"items":rows,"next_cursor":_cursor(rows[-1]["rank"],scope) if more else None})

    @app.get("/api/v1/lifecycles/{boot_id}/snapshots/{snapshot_id}/users",response_model=UserPage)
    async def users(request:Request,boot_id:str,snapshot_id:int):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id)
        if not connection.execute("SELECT 1 FROM snapshot WHERE lifecycle_id=? AND id=?",(lc["id"],snapshot_id)).fetchone(): raise HTTPException(404,"NOT_FOUND")
        rows=[json.loads(x[0]) for x in connection.execute("SELECT data_json FROM user_sample WHERE snapshot_id=?",(snapshot_id,))]
        rows.sort(key=lambda x:x.get("rss_bytes") or 0,reverse=True)
        return _response(r,{**lifecycle_payload(lc),"snapshot_id":snapshot_id,"items":rows})

    @app.get("/api/v1/lifecycles/{boot_id}/series",response_model=SeriesResponse)
    async def series(request:Request,boot_id:str,metrics:str,from_ms:int|None=None,to_ms:int|None=None,resolution:str="raw"):
        if resolution not in {"raw","10s","1m","5m"}: raise HTTPException(422,"INVALID_ARGUMENT")
        requested=[x for x in metrics.split(",") if x]
        if not requested or len(requested)>20: raise HTTPException(422,"INVALID_ARGUMENT")
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id); sql="SELECT s.captured_at_ms,ss.data_json FROM snapshot s JOIN system_sample ss ON ss.snapshot_id=s.id WHERE s.lifecycle_id=?"; params:list[Any]=[lc["id"]]
        if from_ms is not None: sql+=" AND s.captured_at_ms>=?"; params.append(from_ms)
        if to_ms is not None: sql+=" AND s.captured_at_ms<=?"; params.append(to_ms)
        sql+=" ORDER BY s.captured_at_ms"; rows=connection.execute(sql,params).fetchall()
        buckets={"raw":0,"10s":10000,"1m":60000,"5m":300000}; width=buckets[resolution]; sampled=[]
        for row in rows:
            point=(row["captured_at_ms"],json.loads(row["data_json"]));bucket=row["captured_at_ms"]//width if width else row["captured_at_ms"]
            if width and sampled and sampled[-1][0]//width==bucket: sampled[-1]=point
            else: sampled.append(point)
        if len(sampled)>5000: raise HTTPException(413,"QUERY_TOO_LARGE")
        units={m:("percent" if m.endswith("percent") else "bytes" if m.endswith("bytes") else "value") for m in requested}
        payload=[]
        expected=width or settings.sample_interval_ms
        for metric in requested:
            points=[];previous=None
            for ts,data in sampled:
                if previous is not None and ts-previous>expected*2:points.append([previous+expected,None])
                points.append([ts,data.get(metric)]);previous=ts
            payload.append({"metric":metric,"unit":units[metric],"points":points})
        return _response(r,{**lifecycle_payload(lc),"requested_resolution":resolution,"actual_resolution":resolution,"series":payload})

    @app.get("/api/v1/lifecycles/{boot_id}/compare",response_model=CompareResponse)
    async def compare(request:Request,boot_id:str,left_snapshot_id:int,right_snapshot_id:int):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id); left=full_snapshot(connection,lc["id"],left_snapshot_id); right=full_snapshot(connection,lc["id"],right_snapshot_id)
        metrics=set(left["system"])|set(right["system"]); deltas=[]
        for metric in sorted(metrics):
            a,b=left["system"].get(metric),right["system"].get(metric); delta=b-a if isinstance(a,(int,float)) and isinstance(b,(int,float)) else None
            percent=delta*100/a if delta is not None and a else None
            deltas.append({"metric":metric,"unit":"percent" if metric.endswith("percent") else "bytes" if metric.endswith("bytes") else "value","left":a,"right":b,"absolute_delta":delta,"percent_delta":percent})
        def key(p): return (p["pid"],p["create_time_ms"])
        lm={key(p):p for p in left["processes"]}; rm={key(p):p for p in right["processes"]}
        created=[p for k,p in rm.items() if k not in lm]; exited=[p for k,p in lm.items() if k not in rm]; continued=[]
        for k in lm.keys()&rm.keys():
            continued.append({"identity":{"pid":k[0],"create_time_ms":k[1]},"left":lm[k],"right":rm[k]})
        lpids={p["pid"]:p for p in left["processes"]}; rpids={p["pid"]:p for p in right["processes"]}; reused=[{"pid":pid,"left_create_time_ms":lpids[pid]["create_time_ms"],"right_create_time_ms":rpids[pid]["create_time_ms"]} for pid in lpids.keys()&rpids.keys() if lpids[pid]["create_time_ms"]!=rpids[pid]["create_time_ms"]]
        lu={u["uid"]:u for u in left["users"]};ru={u["uid"]:u for u in right["users"]};user_deltas=[]
        for uid in sorted(lu.keys()|ru.keys()):
            a,b=lu.get(uid,{}),ru.get(uid,{}); item={"uid":uid,"username":b.get("username") or a.get("username")}
            for metric in ("cpu_percent","rss_bytes","swap_bytes","read_bps","write_bps","process_count","thread_count"):
                av,bv=a.get(metric),b.get(metric);item[metric+"_left"]=av;item[metric+"_right"]=bv;item[metric+"_delta"]=(bv-av) if isinstance(av,(int,float)) and isinstance(bv,(int,float)) else None
            user_deltas.append(item)
        return _response(r,{**lifecycle_payload(lc),"left_snapshot_id":left_snapshot_id,"right_snapshot_id":right_snapshot_id,"system_deltas":deltas,"processes":{"created":created,"exited":exited,"continued":continued,"pid_reused":reused},"users":user_deltas})

    @app.get("/api/v1/lifecycles/{boot_id}/processes/{pid}",response_model=ProcessHistoryResponse)
    async def process_history(request:Request,boot_id:str,pid:int,create_time_ms:int,from_ms:int|None=None,to_ms:int|None=None,limit:int=Query(100,ge=1,le=1000),cursor:str|None=None):
        r=repo(request); connection=db(request); lc=lifecycle_row(r,connection,boot_id)
        scope=f"process-history:{boot_id}:{pid}:{create_time_ms}:{from_ms}:{to_ms}"; cursor_snapshot_id=_decode_cursor(cursor,scope)
        sql="SELECT p.*,s.captured_at_ms FROM process_sample p JOIN snapshot s ON s.id=p.snapshot_id WHERE s.lifecycle_id=? AND p.pid=? AND p.create_time_ms=?"; params:list[Any]=[lc["id"],pid,create_time_ms]
        if from_ms is not None: sql+=" AND s.captured_at_ms>=?";params.append(from_ms)
        if to_ms is not None: sql+=" AND s.captured_at_ms<=?";params.append(to_ms)
        if cursor_snapshot_id is not None: sql+=" AND p.snapshot_id>?";params.append(cursor_snapshot_id)
        sql+=" ORDER BY s.captured_at_ms,p.snapshot_id LIMIT ?";params.append(limit+1)
        rows=[dict(x) for x in connection.execute(sql,params)]; more=len(rows)>limit;rows=rows[:limit]
        if not rows: raise HTTPException(404,"NOT_FOUND")
        for row in rows: row["truncated"]=bool(row["truncated"])
        return _response(r,{**lifecycle_payload(lc),"pid":pid,"create_time_ms":create_time_ms,"items":rows,"next_cursor":_cursor(rows[-1]["snapshot_id"],scope) if more else None})

    return app
