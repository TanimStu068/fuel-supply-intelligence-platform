from __future__ import annotations
import asyncio, os, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from .config import settings, SCENARIO_FILE
from .db import Base, engine, SessionLocal, get_db
from .models import SimulationInstance, Region, Depot, Station, Route, SupplyArrival, DemandObservation, Allocation, DomainEvent, Fault, AuditLog
from .schemas import AllocationCreate, EventCreate, FaultCreate
from .scenario import reset_from_scenario, load_yaml
from .engine import tick_once, audit
from .bus import bus
from .faults import apply_request_faults, active_faults, has_active_fault

app=FastAPI(title="Fuel Supply Simulator", version="1.0.0", description="Deterministic local fuel-supply simulation environment.")
runner_task=None

@app.middleware("http")
async def fault_middleware(request:Request, call_next):
    if request.url.path.startswith("/admin") or request.url.path=="/v1/health": return await call_next(request)
    db=SessionLocal()
    try:
        response=await apply_request_faults(db,request)
        if response: return response
        response=await call_next(request)
        # If a stale_data fault is currently active, signal staleness on participant GETs.
        if request.method=="GET" and request.url.path.startswith("/v1/"):
            if has_active_fault(db,"stale_data"):
                response.headers["X-Simulator-Stale"]="true"
        return response
    finally: db.close()

def serialize(obj):
    d={}
    for c in obj.__table__.columns:
        v=getattr(obj,c.name)
        if isinstance(v,datetime): v=v.isoformat()
        d[c.name]=v
    return d

async def background_runner():
    # Wall-clock tick loop. Runs forever; only does work while status=="RUNNING".
    # Sleep between ticks = 1 / SIMULATION_SPEED, clamped to [0.05s, 10s].
    # Each tick advances sim_time by SimulationInstance.tick_minutes (set from
    # the TICK_MINUTES env var, defaulting to whatever the scenario YAML says).
    while True:
        await asyncio.sleep(max(0.05, 1.0/max(0.1,settings.simulation_speed)))
        db=SessionLocal()
        try:
            inst=db.get(SimulationInstance,1)
            if inst and inst.status=="RUNNING":
                result=tick_once(db,load_yaml(SCENARIO_FILE)); await bus.publish("simulation.tick",result)
        except Exception as e:
            db.rollback(); await bus.publish("simulator.notice",{"level":"error","message":str(e)})
        finally: db.close()

@app.on_event("startup")
async def startup():
    global runner_task
    Base.metadata.create_all(engine)
    db=SessionLocal()
    try:
        if not db.get(SimulationInstance,1): reset_from_scenario(db,SCENARIO_FILE)
        inst=db.get(SimulationInstance,1)
        # tick_minutes is env-only (TICK_MINUTES). Always overwrite the DB value
        # at startup so the UI and engine reflect the current env setting,
        # regardless of any previous value (whether from an older env, a
        # scenario YAML, or the default fallback).
        inst.tick_minutes = settings.tick_minutes
        if settings.simulator_start_mode.lower()=="running": inst.status="RUNNING"
        db.commit()
    finally: db.close()
    runner_task=asyncio.create_task(background_runner())

@app.on_event("shutdown")
async def shutdown():
    if runner_task: runner_task.cancel()

@app.get("/")
def root(): return {"name":"Fuel Supply Simulator","version":"1.0.0","docs":"/docs","admin":"/admin"}

@app.get("/v1/health")
def health(db:Session=Depends(get_db)):
    inst=db.get(SimulationInstance,1)
    return {"status":"ok","database":"ok","simulation":{"status":inst.status if inst else "UNINITIALIZED","tick":inst.tick if inst else None}}

@app.get("/v1/instance")
def instance(db:Session=Depends(get_db)): return serialize(db.get(SimulationInstance,1))
@app.get("/v1/regions")
def regions(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(Region).all()]
@app.get("/v1/depots")
def depots(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(Depot).all()]
@app.get("/v1/depots/{entity_id}")
def depot(entity_id:str,db:Session=Depends(get_db)):
    x=db.get(Depot,entity_id)
    if not x: raise HTTPException(404,detail={"code":"NOT_FOUND"})
    return serialize(x)
@app.get("/v1/stations")
def stations(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(Station).all()]
@app.get("/v1/stations/{entity_id}")
def station(entity_id:str,db:Session=Depends(get_db)):
    x=db.get(Station,entity_id)
    if not x: raise HTTPException(404,detail={"code":"NOT_FOUND"})
    return serialize(x)
@app.get("/v1/routes")
def routes(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(Route).all()]
@app.get("/v1/supply-arrivals")
def supplies(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(SupplyArrival).order_by(SupplyArrival.planned_tick).all()]
@app.get("/v1/events")
def events(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(DomainEvent).order_by(DomainEvent.id.desc()).all()]
@app.get("/v1/allocations")
def allocations(db:Session=Depends(get_db)): return [serialize(x) for x in db.query(Allocation).order_by(Allocation.id.desc()).all()]
@app.get("/v1/demand-history")
def demand_history(station_id:str|None=None,limit:int=200,db:Session=Depends(get_db)):
    q=db.query(DemandObservation)
    if station_id: q=q.filter(DemandObservation.station_id==station_id)
    return [serialize(x) for x in q.order_by(DemandObservation.id.desc()).limit(min(max(limit,1),2000)).all()]

@app.get("/v1/metrics")
def metrics(db:Session=Depends(get_db)):
    served=float(db.query(func.coalesce(func.sum(DemandObservation.served_liters),0)).scalar())
    unmet=float(db.query(func.coalesce(func.sum(DemandObservation.unmet_liters),0)).scalar())
    total=served+unmet
    allocated=float(db.query(func.coalesce(func.sum(Allocation.quantity),0)).filter(Allocation.status.in_(["IN_TRANSIT","ARRIVED"])).scalar())
    failures=db.query(Allocation).filter(Allocation.status=="FAILED").count()
    return {"served_demand_liters":round(served,3),"unmet_demand_liters":round(unmet,3),"service_level":round(served/total,6) if total else 1.0,"allocation_liters":round(allocated,3),"allocation_failures":failures}

@app.post("/v1/allocations",status_code=201)
async def create_allocation(req:AllocationCreate,db:Session=Depends(get_db)):
    existing=db.query(Allocation).filter(Allocation.idempotency_key==req.idempotency_key).first()
    if existing:
        if (existing.source_depot_id==req.source_depot_id
            and existing.destination_station_id==req.destination_station_id
            and existing.route_id==req.route_id
            and existing.fuel_type==req.fuel_type
            and float(existing.quantity)==float(req.quantity)):
            return serialize(existing)
        raise HTTPException(409, detail={"code":"IDEMPOTENCY_KEY_MISMATCH","message":"Idempotency key was already used with different parameters."})
    inst=db.get(SimulationInstance,1); depot=db.get(Depot,req.source_depot_id); station=db.get(Station,req.destination_station_id); route=db.get(Route,req.route_id)
    def bad(code,msg,status=409): raise HTTPException(status,detail={"code":code,"message":msg})
    if not depot: bad("NOT_FOUND","Source depot not found",404)
    if not station: bad("NOT_FOUND","Destination station not found",404)
    if not route: bad("NOT_FOUND","Route not found",404)
    if route.source_depot_id!=depot.id or route.destination_station_id!=station.id: bad("ROUTE_MISMATCH","Route does not connect selected depot and station")
    if depot.status not in ("OPEN","CONSTRAINED"): bad("DEPOT_CLOSED","Depot unavailable")
    if station.status!="OPEN": bad("STATION_CLOSED","Station unavailable")
    if route.status!="AVAILABLE": bad("ROUTE_DISRUPTED","Route unavailable")
    if req.quantity>route.max_shipment: bad("ROUTE_CAPACITY_EXCEEDED","Quantity exceeds route max shipment")
    inv=dict(depot.inventory)
    if float(inv[req.fuel_type])<req.quantity: bad("INSUFFICIENT_INVENTORY","Depot does not have enough inventory")
    in_flight = float(db.query(func.coalesce(func.sum(Allocation.quantity),0)).filter(
        Allocation.source_depot_id==depot.id,
        Allocation.created_tick==inst.tick,
        Allocation.status.in_(["PENDING","IN_TRANSIT"])
    ).scalar())
    if in_flight + req.quantity > depot.dispatch_capacity_per_tick:
        bad("DISPATCH_CAPACITY_EXCEEDED","Depot dispatch capacity per tick exceeded")
    projected=float(station.inventory[req.fuel_type])+req.quantity
    if projected>float(station.capacity[req.fuel_type]): bad("DESTINATION_CAPACITY_EXCEEDED","Shipment would exceed destination capacity")
    inv[req.fuel_type]=round(float(inv[req.fuel_type])-req.quantity,3); depot.inventory=inv
    row=Allocation(idempotency_key=req.idempotency_key,source_depot_id=depot.id,destination_station_id=station.id,route_id=route.id,fuel_type=req.fuel_type,quantity=req.quantity,created_tick=inst.tick,status="PENDING")
    db.add(row); db.flush(); audit(db,"allocation.created","allocation",row.id,metadata={"quantity":req.quantity,"fuel_type":req.fuel_type}); db.commit(); db.refresh(row)
    await bus.publish("allocation.status_changed",serialize(row)); await bus.publish("inventory.updated",{"entity_type":"depot","entity_id":depot.id,"inventory":depot.inventory})
    return serialize(row)

@app.post("/v1/allocations/{allocation_id}/cancel")
async def cancel_allocation(allocation_id:int,db:Session=Depends(get_db)):
    a=db.get(Allocation,allocation_id)
    if not a: raise HTTPException(404,detail={"code":"ALLOCATION_NOT_FOUND"})
    if a.status!="PENDING": raise HTTPException(409,detail={"code":"CANNOT_CANCEL","message":"Only pending allocations can be cancelled"})
    d=db.get(Depot,a.source_depot_id); inv=dict(d.inventory); inv[a.fuel_type]=round(float(inv[a.fuel_type])+a.quantity,3); d.inventory=inv; a.status="CANCELLED"; audit(db,"allocation.cancelled","allocation",a.id); db.commit(); await bus.publish("allocation.status_changed",serialize(a)); await bus.publish("inventory.updated",{"entity_type":"depot","entity_id":d.id,"inventory":d.inventory}); return serialize(a)

@app.get("/v1/stream")
async def stream(db:Session=Depends(get_db)):
    faults=active_faults(db)
    if any(f.type=="stream_disconnect" for f in faults): raise HTTPException(503,detail={"code":"FAULT_INJECTED"})
    q=bus.subscribe()
    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try: p=await asyncio.wait_for(q.get(),timeout=15); yield bus.encode(p)
                except asyncio.TimeoutError: yield ": keepalive\n\n"
        finally: bus.unsubscribe(q)
    return StreamingResponse(gen(),media_type="text/event-stream",headers={"Cache-Control":"no-cache"})

@app.post("/admin/run")
async def admin_run(db:Session=Depends(get_db)):
    inst=db.get(SimulationInstance,1); inst.status="RUNNING"; audit(db,"admin.run"); db.commit(); return serialize(inst)
@app.post("/admin/pause")
async def admin_pause(db:Session=Depends(get_db)):
    inst=db.get(SimulationInstance,1); inst.status="PAUSED"; audit(db,"admin.pause"); db.commit(); return serialize(inst)
@app.post("/admin/toggle")
async def admin_toggle(db:Session=Depends(get_db)):
    """Flip the simulation between RUNNING and PAUSED."""
    inst=db.get(SimulationInstance,1)
    if inst.status=="RUNNING":
        inst.status="PAUSED"; audit(db,"admin.pause")
    else:
        inst.status="RUNNING"; audit(db,"admin.run")
    db.commit()
    return serialize(inst)
@app.post("/admin/step")
async def admin_step(db:Session=Depends(get_db)):
    result=tick_once(db,load_yaml(SCENARIO_FILE)); await bus.publish("simulation.tick",result); return result
@app.post("/admin/reset")
async def admin_reset(db:Session=Depends(get_db)):
    reset_from_scenario(db,SCENARIO_FILE); await bus.publish("simulator.notice",{"message":"Simulation reset"}); return serialize(db.get(SimulationInstance,1))
@app.post("/admin/events",status_code=201)
async def admin_event(req:EventCreate,db:Session=Depends(get_db)):
    row=DomainEvent(type=req.type,start_tick=req.start_tick,end_tick=req.start_tick+req.duration_ticks,status="SCHEDULED",parameters=req.parameters); db.add(row); db.flush(); audit(db,"event.created","event",row.id,metadata={"type":req.type}); db.commit(); db.refresh(row); return serialize(row)
@app.post("/admin/faults",status_code=201)
def admin_fault(req:FaultCreate,db:Session=Depends(get_db)):
    now=datetime.now(timezone.utc); row=Fault(type=req.type,start_wall_time=now,end_wall_time=now+timedelta(seconds=req.duration_seconds),active=True,parameters=req.parameters); db.add(row); db.flush(); audit(db,"fault.created","fault",row.id,metadata={"type":req.type}); db.commit(); return serialize(row)
@app.post("/admin/faults/clear")
def clear_faults(db:Session=Depends(get_db)):
    db.query(Fault).filter(Fault.active.is_(True)).update({Fault.active:False},synchronize_session=False); audit(db,"fault.clear_all"); db.commit(); return {"status":"cleared"}
@app.get("/admin/audit")
def admin_audit(limit:int=200,db:Session=Depends(get_db)): return [serialize(x) for x in db.query(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit,1000)).all()]

@app.get("/admin/stats")
def admin_stats(db:Session=Depends(get_db)):
    """Return row counts and DB file size for the 'data stored' panel."""
    import os as _os
    counts={
        "regions": db.query(Region).count(),
        "depots": db.query(Depot).count(),
        "stations": db.query(Station).count(),
        "routes": db.query(Route).count(),
        "supply_arrivals": db.query(SupplyArrival).count(),
        "demand_observations": db.query(DemandObservation).count(),
        "allocations": db.query(Allocation).count(),
        "events": db.query(DomainEvent).count(),
        "faults": db.query(Fault).count(),
        "audit_logs": db.query(AuditLog).count(),
    }
    db_path=None; db_bytes=None
    try:
        url=settings.database_url
        if url.startswith("sqlite:///"):
            db_path=url[len("sqlite:///"):]
            if db_path and _os.path.exists(db_path):
                db_bytes=_os.path.getsize(db_path)
    except Exception:
        pass
    return {"row_counts":counts,"db_path":db_path,"db_bytes":db_bytes,
            "database_url":settings.database_url,"scenario_file":SCENARIO_FILE}

@app.get("/admin/faults")
def admin_list_faults(db:Session=Depends(get_db)):
    return [serialize(f) for f in db.query(Fault).order_by(Fault.id.desc()).limit(50).all()]

@app.get("/admin/events")
def admin_list_events(db:Session=Depends(get_db)):
    return [serialize(e) for e in db.query(DomainEvent).order_by(DomainEvent.id.desc()).limit(50).all()]

@app.get("/admin",response_class=HTMLResponse)
def admin_page(db:Session=Depends(get_db)):
    # Server-side render initial state so the page is informative even before JS runs.
    inst=db.get(SimulationInstance,1)
    served=float(db.query(func.coalesce(func.sum(DemandObservation.served_liters),0)).scalar())
    unmet=float(db.query(func.coalesce(func.sum(DemandObservation.unmet_liters),0)).scalar())
    total=served+unmet
    allocated=float(db.query(func.coalesce(func.sum(Allocation.quantity),0)).filter(Allocation.status.in_(["IN_TRANSIT","ARRIVED"])).scalar())
    failures=db.query(Allocation).filter(Allocation.status=="FAILED").count()
    metrics={"served_demand_liters":round(served,3),"unmet_demand_liters":round(unmet,3),"service_level":round(served/total,6) if total else 1.0,"allocation_liters":round(allocated,3),"allocation_failures":failures}
    audit_rows=db.query(AuditLog).order_by(AuditLog.id.desc()).limit(10).all()
    audit_data=[serialize(x) for x in audit_rows]
    import json as _json, os as _os
    inst_json=_json.dumps(serialize(inst),indent=2) if inst else "{}"
    metrics_json=_json.dumps(metrics,indent=2)
    audit_json=_json.dumps(audit_data,indent=2)
    # Initial row counts so the panel isn't empty until first JS refresh.
    row_counts={
        "regions": db.query(Region).count(),
        "depots": db.query(Depot).count(),
        "stations": db.query(Station).count(),
        "routes": db.query(Route).count(),
        "supply_arrivals": db.query(SupplyArrival).count(),
        "demand_observations": db.query(DemandObservation).count(),
        "allocations": db.query(Allocation).count(),
        "events": db.query(DomainEvent).count(),
        "faults": db.query(Fault).count(),
        "audit_logs": db.query(AuditLog).count(),
    }
    db_path=None; db_bytes=None
    try:
        if settings.database_url.startswith("sqlite:///"):
            db_path=settings.database_url[len("sqlite:///"):]
            if db_path and _os.path.exists(db_path):
                db_bytes=_os.path.getsize(db_path)
    except Exception:
        pass
    stats={"row_counts":row_counts,"db_path":db_path,"db_bytes":db_bytes,
           "database_url":settings.database_url,"scenario_file":SCENARIO_FILE}
    stats_json=_json.dumps(stats,indent=2)

    html=ADMIN_HTML_TEMPLATE.replace("__INST_JSON__", inst_json) \
                            .replace("__METRICS_JSON__", metrics_json) \
                            .replace("__AUDIT_JSON__", audit_json) \
                            .replace("__STATS_JSON__", stats_json)
    return HTMLResponse(html, headers={"Cache-Control":"no-store, no-cache, must-revalidate"})


ADMIN_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fuel Simulator Admin</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0; background: #0e1116; color: #e6edf3; }
  header { padding: 14px 22px; background: #161b22; border-bottom: 1px solid #30363d;
           display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap; }
  header h1 { font-size: 18px; margin: 0; }
  .status-pill { display:inline-flex; align-items:center; gap:6px;
                 padding: 4px 10px; border-radius: 999px; font-size: 12px;
                 background: #21262d; margin-left: 6px; }
  .status-pill .dot { width:8px; height:8px; border-radius:50%; background:#8b949e; }
  .status-pill.running { background:#1f6f3a; } .status-pill.running .dot { background:#3fb950;
                                                                       box-shadow:0 0 6px #3fb950; }
  .status-pill.paused  { background:#8a6d3b; } .status-pill.paused  .dot { background:#f7c948; }
  .status-pill.error   { background:#7a1f1f; } .status-pill.error   .dot { background:#ff6b6b; }
  main { padding: 16px 22px; display: grid; gap: 16px;
         grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  section.card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 16px; }
  section.card h2 { margin:0 0 10px; font-size:14px; color:#c9d1d9; text-transform:uppercase;
                    letter-spacing:.06em; border-bottom:1px solid #30363d; padding-bottom:6px; }
  label { display:block; margin:.5rem 0 .15rem; font-size:.85rem; color:#8b949e; }
  input,select,textarea { width:100%; padding:.45rem .55rem; border-radius:6px; border:1px solid #30363d;
                          background:#0d1117; color:#e6edf3; font-family:inherit; font-size:13px; }
  textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .btn-row { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.6rem; }
  button { padding:.5rem .95rem; border-radius:6px; border:1px solid #30363d;
           background:#21262d; color:#e6edf3; cursor:pointer; font-size:13px; font-weight:500;
           transition: background .15s, transform .05s; }
  button:hover { background:#30363d; }
  button:active { transform: translateY(1px); }
  button.primary { background:#1f6f3a; border-color:#238636; }
  button.primary:hover { background:#238636; }
  button.danger  { background:#7a1f1f; border-color:#b62324; }
  button.danger:hover { background:#b62324; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  pre { background:#0d1117; border:1px solid #21262d; border-radius:6px; padding:10px;
        overflow:auto; max-height:240px; font-size:12px; color:#c9d1d9; margin:.5rem 0 0; }
  .toast { position: fixed; right: 18px; bottom: 18px; min-width: 260px; max-width: 360px;
           padding: 12px 14px; border-radius: 8px; font-size: 13px; box-shadow: 0 8px 24px rgba(0,0,0,.4);
           opacity: 0; transform: translateY(8px); transition: opacity .2s, transform .2s;
           pointer-events: none; z-index: 9999; }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.success { background: #1f6f3a; color:#fff; }
  .toast.error   { background: #7a1f1f; color:#fff; }
  .toast.info    { background: #1f6feb; color:#fff; }
  .kv { display:grid; grid-template-columns: max-content 1fr; gap:.3rem 1rem; font-size:13px; }
  .kv .k { color:#8b949e; }
  .kv .v { color:#e6edf3; font-variant-numeric: tabular-nums; }
  table { width:100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align:left; padding:5px 6px; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-weight:600; }
  .pill { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px;
          background:#21262d; color:#c9d1d9; }
  .pill.live { background:#1f6f3a; color:#fff; }
  .refresh-dot { display:inline-block; width:7px; height:7px; border-radius:50%;
                 background:#3fb950; margin-right:6px; animation: pulse 1.6s infinite; }
  @keyframes pulse { 0%{opacity:1} 50%{opacity:.35} 100%{opacity:1} }
  footer { padding: 12px 22px; color:#8b949e; font-size:12px; border-top:1px solid #30363d; }
  a { color:#58a6ff; }
</style>
</head>
<body>
<header>
  <div>
    <h1>⛽ Fuel Supply Simulator </h1>
    <div style="font-size:12px; color:#8b949e; margin-top:4px;">
      Local admin control panel
    </div>
  </div>
  <div>
    <span class="status-pill" id="connPill"><span class="dot"></span><span id="connText">checking…</span></span>
    <span class="status-pill" id="simPill"><span class="dot"></span><span id="simText">—</span></span>
    <span class="status-pill"><span class="refresh-dot"></span>auto-refresh 2s</span>
  </div>
</header>

<main>
  <section class="card">
    <h2>Simulator status</h2>
    <div class="kv">
      <div class="k">scenario</div><div class="v" id="i-scenario">—</div>
      <div class="k">version</div><div class="v" id="i-version">—</div>
      <div class="k">tick</div><div class="v" id="i-tick">—</div>
      <div class="k">sim_time</div><div class="v" id="i-time">—</div>
      <div class="k">status</div><div class="v" id="i-status">—</div>
      <div class="k">tick_minutes</div><div class="v" id="i-tickmin">—</div>
    </div>
    <div class="btn-row">
      <button id="btn-toggle" class="primary" data-a="toggle">▶ Run</button>
      <button id="btn-step" data-a="step">⏭ Step one tick</button>
      <button id="btn-reset" class="danger" data-a="reset">⟲ Reset to scenario</button>
    </div>
    <div id="control-feedback" style="margin-top:.6rem; font-size:12px; color:#8b949e;">
      Click any button — you'll see the response here.
    </div>
  </section>

  <section class="card">
    <h2>Live metrics</h2>
    <div class="kv">
      <div class="k">served_demand_liters</div><div class="v" id="m-served">0</div>
      <div class="k">unmet_demand_liters</div><div class="v" id="m-unmet">0</div>
      <div class="k">service_level</div><div class="v" id="m-svc">—</div>
      <div class="k">allocation_liters (in transit / arrived)</div><div class="v" id="m-alloc">0</div>
      <div class="k">allocation_failures</div><div class="v" id="m-fail">0</div>
    </div>
    <details style="margin-top:.6rem;">
      <summary style="cursor:pointer; color:#8b949e; font-size:12px;">Raw JSON</summary>
      <pre id="metricsJson">__METRICS_JSON__</pre>
    </details>
  </section>

  <section class="card">
    <h2>Data stored</h2>
    <table style="margin-top:.7rem;">
      <thead><tr><th>Table</th><th style="text-align:right">Rows</th></tr></thead>
      <tbody id="statsRows"></tbody>
    </table>
  </section>

  <section class="card">
    <h2>Inject event</h2>
    <label>Type
      <select id="evt-type">
        <option value="demand_spike">demand_spike</option>
        <option value="shipment_delay">shipment_delay</option>
        <option value="route_disruption">route_disruption</option>
        <option value="station_outage">station_outage</option>
        <option value="depot_constraint">depot_constraint</option>
        <option value="supply_shortfall">supply_shortfall</option>
      </select>
    </label>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:.6rem;">
      <div><label>Start tick<input id="evt-start" type="number" value="0"></label></div>
      <div><label>Duration ticks<input id="evt-duration" type="number" value="4"></label></div>
    </div>
    <label>Parameters (JSON)
      <textarea id="evt-params" rows="3" spellcheck="false">{"multiplier": 1.5}</textarea>
    </label>
    <div id="evt-params-hint" style="font-size:11px; color:#8b949e; margin-top:-.2rem;">Default multiplier 1.5× applied to all stations. Optionally scope via <code>station_ids</code> or <code>region_ids</code>.</div>
    <div class="btn-row">
      <button id="evt-go" class="primary">Inject event</button>
    </div>
    <pre id="evt-out">No event injected yet.</pre>
  </section>

  <section class="card">
    <h2>Inject fault</h2>
    <label>Type
      <select id="ft-type">
        <option value="latency">latency</option>
        <option value="unavailable">unavailable</option>
        <option value="error_rate">error_rate</option>
        <option value="stale_data">stale_data</option>
        <option value="stream_disconnect">stream_disconnect</option>
      </select>
    </label>
    <div><label>Duration (seconds)<input id="ft-duration" type="number" value="60"></label></div>
    <label>Parameters (JSON)
      <textarea id="ft-params" rows="3" spellcheck="false">{"delay_ms": 1000}</textarea>
    </label>
    <div id="ft-params-hint" style="font-size:11px; color:#8b949e; margin-top:-.2rem;">Default 1000&nbsp;ms added to every request. Type-specific params: <code>rate</code> (error_rate), or <code>{}</code> for unavailable / stale_data / stream_disconnect.</div>
    <div class="btn-row">
      <button id="ft-go" class="primary">Inject fault</button>
      <button id="ft-clear" class="danger">Clear all faults</button>
    </div>
    <pre id="ft-out">No fault injected yet.</pre>
  </section>

  <section class="card" style="grid-column: 1 / -1;">
    <h2>Recent audit log (last 10)</h2>
    <pre id="audit">__AUDIT_JSON__</pre>
  </section>

  <section class="card" style="grid-column: 1 / -1;">
    <h2>Raw /v1/instance</h2>
    <pre id="instJson">__INST_JSON__</pre>
  </section>
</main>

<footer>
  <a href="/docs">Swagger API</a> · <a href="/redoc">ReDoc</a> ·
  endpoints: <code>/v1/health</code>, <code>/v1/metrics</code>, <code>/v1/instance</code>,
  <code>/admin/audit</code>, <code>/admin/stats</code>, <code>/admin/faults</code>,
  <code>/admin/events</code>
</footer>

<div class="toast" id="toast"></div>

<script>
(function(){
  function $(s, el){ return (el||document).querySelector(s); }
  function $all(s, el){ return Array.from((el||document).querySelectorAll(s)); }
  function showToast(msg, kind){
    var t=$('#toast'); t.className='toast '+ (kind||'info'); t.textContent=msg;
    requestAnimationFrame(function(){ t.classList.add('show'); });
    clearTimeout(showToast._t);
    showToast._t=setTimeout(function(){ t.classList.remove('show'); }, 3500);
  }
  function fmtBytes(n){
    if (n==null) return '—';
    if (n < 1024) return n+' B';
    if (n < 1024*1024) return (n/1024).toFixed(1)+' KB';
    return (n/(1024*1024)).toFixed(2)+' MB';
  }
  function safeFetch(url, opts){
    opts = opts || {};
    return fetch(url, opts).then(function(r){
      return r.text().then(function(txt){
        var data; try { data = JSON.parse(txt); } catch(e) { data = txt; }
        return { status: r.status, ok: r.ok, data: data };
      });
    }).catch(function(e){
      return { status: 0, ok: false, data: String(e) };
    });
  }
  function setText(id, val){ var el=$(id); if (el) el.textContent = val; }
  function setStatusPill(elId, textId, klass, text){
    var p=$(elId); if(!p) return; p.className='status-pill '+(klass||'');
    var t=$(textId); if(t) t.textContent=text;
  }

  function refreshInstance(){
    return safeFetch('/v1/instance').then(function(r){
      if (!r.ok) {
        setStatusPill('#connPill','#connText','error','API error '+r.status);
        setText('#instJson','ERROR '+r.status+': '+JSON.stringify(r.data));
        return;
      }
      setStatusPill('#connPill','#connText','running','API connected');
      var d=r.data||{};
      setText('#i-scenario', d.scenario_id||'—');
      setText('#i-version',  d.scenario_version||'—');
      setText('#i-tick',     d.tick!=null? d.tick : '—');
      setText('#i-time',     d.sim_time||'—');
      setText('#i-tickmin',  d.tick_minutes!=null? d.tick_minutes : '—');
      var st=d.status||'UNKNOWN';
      setText('#i-status', st);
      var sk = (st==='RUNNING') ? 'running' : (st==='PAUSED' ? 'paused' : 'error');
      setStatusPill('#simPill','#simText', sk, 'sim: '+st.toLowerCase());
      updateToggleButton(st);
      setText('#instJson', JSON.stringify(d,null,2));
    });
  }
  function updateToggleButton(status){
    var b=$('#btn-toggle'); if (!b) return;
    if (status==='RUNNING'){
      b.textContent='⏸ Pause';
      b.className='btn-toggle';
    } else {
      b.textContent='▶ Run';
      b.className='btn-toggle primary';
    }
  }
  function refreshMetrics(){
    return safeFetch('/v1/metrics').then(function(r){
      if (!r.ok) {
        setText('#metricsJson','ERROR '+r.status+': '+JSON.stringify(r.data));
        return;
      }
      var d=r.data||{};
      setText('#m-served', (d.served_demand_liters||0).toLocaleString());
      setText('#m-unmet',  (d.unmet_demand_liters||0).toLocaleString());
      setText('#m-svc',    d.service_level!=null? (d.service_level*100).toFixed(2)+'%' : '—');
      setText('#m-alloc',  (d.allocation_liters||0).toLocaleString());
      setText('#m-fail',   d.allocation_failures!=null? d.allocation_failures : '—');
      setText('#metricsJson', JSON.stringify(d,null,2));
    });
  }
  function refreshStats(){
    return safeFetch('/admin/stats').then(function(r){
      if (!r.ok) return;
      var d=r.data||{};
      var rows=d.row_counts||{};
      var keys=Object.keys(rows);
      var html=keys.map(function(k){
        return '<tr><td>'+k+'</td><td style="text-align:right">'+(rows[k]||0).toLocaleString()+'</td></tr>';
      }).join('') || '<tr><td colspan="2">no data</td></tr>';
      setText('#statsRows', ''); $('#statsRows').innerHTML = html;
    });
  }
  function refreshAudit(){
    return safeFetch('/admin/audit?limit=10').then(function(r){
      if (!r.ok) {
        setText('#audit','ERROR '+r.status+': '+(typeof r.data==='string'?r.data:JSON.stringify(r.data)));
        return;
      }
      setText('#audit', JSON.stringify(r.data,null,2));
    });
  }
  function refreshAll(){
    refreshInstance();
    refreshMetrics();
    refreshAudit();
    refreshStats();
  }

  function adminAction(action){
    var btn=$('#btn-'+action); if (btn) btn.disabled=true;
    var fb=$('#control-feedback');
    setText('#control-feedback','POST /admin/'+action+' …');
    return safeFetch('/admin/'+action, { method:'POST'})
      .then(function(r){
        var msg = (r.ok ? '✓ /admin/'+action+' → HTTP '+r.status
                        : '✗ /admin/'+action+' → HTTP '+r.status+' — '+(typeof r.data==='string'?r.data:JSON.stringify(r.data)));
        setText('#control-feedback', msg);
        showToast(msg, r.ok?'success':'error');
        refreshAll();
      })
      .finally(function(){ if (btn) btn.disabled=false; });
  }

  function bindAdminButtons(){
    ['toggle','step','reset'].forEach(function(a){
      var b=$('#btn-'+a); if (!b) return;
      b.addEventListener('click', function(){ adminAction(a); });
    });
  }
  function bindEventButton(){
    var btn=$('#evt-go'); if (!btn) return;
    btn.addEventListener('click', function(){
      var params={};
      var raw=$('#evt-params').value||'{}';
      try { params=JSON.parse(raw); }
      catch(e){ setText('#evt-out','Invalid JSON: '+e); showToast('Invalid JSON in event params','error'); return; }
      var body={ type:$('#evt-type').value,
                 start_tick: parseInt($('#evt-start').value,10),
                 duration_ticks: parseInt($('#evt-duration').value,10),
                 parameters: params };
      btn.disabled=true;
      safeFetch('/admin/events',{ method:'POST',
        headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(body)
      }).then(function(r){
        var msg='HTTP '+r.status+'\n'+(typeof r.data==='string'?r.data:JSON.stringify(r.data,null,2));
        setText('#evt-out', msg);
        showToast('Event '+(r.ok?'injected':'failed')+' ('+body.type+')', r.ok?'success':'error');
        refreshAll();
      }).finally(function(){ btn.disabled=false; });
    });
  }
  function bindFaultButton(){
    var btn=$('#ft-go'); if (!btn) return;
    btn.addEventListener('click', function(){
      var params={};
      var raw=$('#ft-params').value||'{}';
      try { params=JSON.parse(raw); }
      catch(e){ setText('#ft-out','Invalid JSON: '+e); showToast('Invalid JSON in fault params','error'); return; }
      var body={ type:$('#ft-type').value,
                 duration_seconds: parseInt($('#ft-duration').value,10),
                 parameters: params };
      btn.disabled=true;
      safeFetch('/admin/faults',{ method:'POST',
        headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(body)
      }).then(function(r){
        var msg='HTTP '+r.status+'\n'+(typeof r.data==='string'?r.data:JSON.stringify(r.data,null,2));
        setText('#ft-out', msg);
        showToast('Fault '+(r.ok?'injected':'failed')+' ('+body.type+')', r.ok?'success':'error');
        refreshAll();
      }).finally(function(){ btn.disabled=false; });
    });
  }
  function bindClearButton(){
    var btn=$('#ft-clear'); if (!btn) return;
    btn.addEventListener('click', function(){
      btn.disabled=true;
      safeFetch('/admin/faults/clear',{ method:'POST',
        headers:{ 'Content-Type':'application/json' }, body:'{}'
      }).then(function(r){
        var msg='HTTP '+r.status+'\n'+(typeof r.data==='string'?r.data:JSON.stringify(r.data,null,2));
        setText('#ft-out', msg);
        showToast(r.ok?'All faults cleared':'Clear failed', r.ok?'success':'error');
        refreshAll();
      }).finally(function(){ btn.disabled=false; });
    });
  }

  // ---------------- parameter presets ----------------
  // When the user changes the Type dropdown, auto-fill the Parameters (JSON)
  // textarea with sensible defaults matching what the engine/fault layer accepts.
  // Users can still edit the JSON freely afterwards.
  var EVENT_PRESETS = {
    'demand_spike':     { params: {"multiplier": 1.5}, hint: 'Multiplies station demand. Optionally scope: <code>station_ids</code>, <code>region_ids</code>.' },
    'shipment_delay':   { params: {"delay_ticks": 2, "depot_ids": ["depot-gazipur", "depot-patiya"], "fuel_types": ["DIESEL", "PETROL", "OCTANE"]}, hint: 'Pushes <code>planned_tick</code> of matching supply arrivals forward. Empty <code>depot_ids</code>/<code>fuel_types</code> = all.' },
    'route_disruption': { params: {"route_ids": ["route-gazipur-mirpur"]}, hint: 'Marks routes <code>DISRUPTED</code> for the duration. Provide one or more <code>route_ids</code>.' },
    'station_outage':   { params: {"station_ids": ["station-mirpur"]}, hint: 'Marks stations <code>OUTAGE</code> for the duration. Their demand becomes 100% unmet.' },
    'depot_constraint': { params: {"depot_ids": ["depot-gazipur"]}, hint: 'Marks depots <code>CONSTRAINED</code>. Dispatch capacity becomes stricter for the duration.' },
    'supply_shortfall': { params: {"factor": 0.5, "depot_ids": [], "fuel_types": []}, hint: 'Multiplies scheduled supply quantities by <code>factor</code>. Empty lists = all depots/fuels.' }
  };
  var FAULT_PRESETS = {
    'latency':           { params: {"delay_ms": 1000}, hint: 'Adds delay (ms) to every request. Default 1000 ms.' },
    'unavailable':       { params: {},                   hint: 'Returns <code>503</code> for every request. No extra params needed.' },
    'error_rate':        { params: {"rate": 0.3},        hint: 'Fraction (<code>0&ndash;1</code>) of requests that return 503. Default 0.3.' },
    'stale_data':        { params: {},                   hint: 'Adds <code>X-Simulator-Stale: true</code> header on GETs. No extra params needed.' },
    'stream_disconnect': { params: {},                   hint: 'Drops SSE clients on <code>/v1/stream</code>. No extra params needed.' }
  };
  function bindEventPresets(){
    var sel=$('#evt-type'), ta=$('#evt-params'), hint=$('#evt-params-hint');
    if (!sel || !ta) return;
    var apply=function(){
      var preset=EVENT_PRESETS[sel.value];
      if (!preset) return;
      ta.value = JSON.stringify(preset.params, null, 2);
      if (hint) hint.innerHTML = preset.hint;
    };
    sel.addEventListener('change', apply);
    apply();
  }
  function bindFaultPresets(){
    var sel=$('#ft-type'), ta=$('#ft-params'), hint=$('#ft-params-hint');
    if (!sel || !ta) return;
    var apply=function(){
      var preset=FAULT_PRESETS[sel.value];
      if (!preset) return;
      ta.value = JSON.stringify(preset.params, null, 2);
      if (hint) hint.innerHTML = preset.hint;
    };
    sel.addEventListener('change', apply);
    apply();
  }

  function init(){
    bindAdminButtons();
    bindEventPresets();
    bindFaultPresets();
    bindEventButton();
    bindFaultButton();
    bindClearButton();
    refreshAll();
    setInterval(refreshAll, 2000);
  }
  if (document.readyState==='loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
</script>
</body>
</html>
"""
