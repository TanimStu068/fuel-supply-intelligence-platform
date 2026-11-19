from __future__ import annotations
from datetime import timedelta, datetime, timezone
import hashlib, random
from sqlalchemy.orm import Session
from .models import SimulationInstance, Region, Depot, Station, Route, SupplyArrival, DemandObservation, Allocation, DomainEvent, AuditLog
from .scenario import load_yaml

FUEL_TYPES=("DIESEL","PETROL","OCTANE")

def audit(db: Session, action: str, entity_type=None, entity_id=None, result="OK", metadata=None):
    inst=db.get(SimulationInstance,1)
    db.add(AuditLog(wall_time=datetime.now(timezone.utc), sim_time=inst.sim_time if inst else None, tick=inst.tick if inst else None, action=action, entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None, result=result, metadata_json=metadata or {}))

def _rng(seed:int,tick:int,entity:str,purpose:str):
    raw=f"{seed}:{tick}:{entity}:{purpose}".encode()
    n=int.from_bytes(hashlib.sha256(raw).digest()[:8],"big")
    return random.Random(n)

def _hour_factor(hour:int, profile:str):
    if profile=="industrial": return 1.55 if 6 <= hour < 18 else 0.45
    if profile=="highway": return 1.35 if 6 <= hour < 10 or 16 <= hour < 21 else 0.75
    if profile=="urban_high": return 1.45 if 7 <= hour < 10 or 16 <= hour < 21 else 0.70
    return 1.25 if 7 <= hour < 21 else 0.65

def start_events(db: Session, inst: SimulationInstance):
    events=db.query(DomainEvent).filter(DomainEvent.status=="SCHEDULED", DomainEvent.start_tick<=inst.tick).all()
    for e in events:
        e.status="ACTIVE"; apply_event_start(db,e); audit(db,"event.started","event",e.id,metadata={"type":e.type})

def apply_event_start(db: Session,e:DomainEvent):
    p=e.parameters or {}
    if e.type=="demand_spike":
        mult=float(p.get("multiplier",1.5)); station_ids=set(p.get("station_ids",[])); region_ids=set(p.get("region_ids",[]))
        q=db.query(Station)
        for s in q.all():
            if (not station_ids and not region_ids) or s.id in station_ids or s.region_id in region_ids:
                s.demand_multiplier=float(s.demand_multiplier)*mult
    elif e.type=="route_disruption":
        for rid in p.get("route_ids",[]):
            r=db.get(Route,rid)
            if r: r.status="DISRUPTED"
    elif e.type=="station_outage":
        for sid in p.get("station_ids",[]):
            s=db.get(Station,sid)
            if s: s.status="OUTAGE"
    elif e.type=="depot_constraint":
        for did in p.get("depot_ids",[]):
            d=db.get(Depot,did)
            if d: d.status="CONSTRAINED"
    elif e.type=="shipment_delay":
        delay=int(p.get("delay_ticks",2)); depot_ids=set(p.get("depot_ids",[])); fuels=set(p.get("fuel_types",[]))
        for a in db.query(SupplyArrival).filter(SupplyArrival.status=="SCHEDULED").all():
            if (not depot_ids or a.depot_id in depot_ids) and (not fuels or a.fuel_type in fuels):
                a.planned_tick += delay; a.status="DELAYED"
    elif e.type=="supply_shortfall":
        factor=float(p.get("factor",0.5)); depot_ids=set(p.get("depot_ids",[])); fuels=set(p.get("fuel_types",[]))
        for a in db.query(SupplyArrival).filter(SupplyArrival.status.in_(["SCHEDULED","DELAYED"])).all():
            if (not depot_ids or a.depot_id in depot_ids) and (not fuels or a.fuel_type in fuels): a.quantity *= factor

def resolve_events(db: Session,inst:SimulationInstance):
    for e in db.query(DomainEvent).filter(DomainEvent.status=="ACTIVE",DomainEvent.end_tick<=inst.tick).all():
        p=e.parameters or {}
        if e.type=="demand_spike":
            mult=float(p.get("multiplier",1.5)); station_ids=set(p.get("station_ids",[])); region_ids=set(p.get("region_ids",[]))
            for s in db.query(Station).all():
                if (not station_ids and not region_ids) or s.id in station_ids or s.region_id in region_ids:
                    s.demand_multiplier=max(0.01,float(s.demand_multiplier)/mult)
        elif e.type=="route_disruption":
            for rid in p.get("route_ids",[]):
                r=db.get(Route,rid)
                if r: r.status="AVAILABLE"
        elif e.type=="station_outage":
            for sid in p.get("station_ids",[]):
                s=db.get(Station,sid)
                if s: s.status="OPEN"
        elif e.type=="depot_constraint":
            for did in p.get("depot_ids",[]):
                d=db.get(Depot,did)
                if d: d.status="OPEN"
        e.status="RESOLVED"; audit(db,"event.resolved","event",e.id,metadata={"type":e.type})

def process_supply(db: Session,inst:SimulationInstance):
    arrivals=db.query(SupplyArrival).filter(SupplyArrival.status.in_(["SCHEDULED","DELAYED"]), SupplyArrival.planned_tick<=inst.tick).all()
    for a in arrivals:
        d=db.get(Depot,a.depot_id); inv=dict(d.inventory); cap=float(d.capacity[a.fuel_type]); room=max(0,cap-float(inv[a.fuel_type])); added=min(room,float(a.quantity))
        inv[a.fuel_type]=round(float(inv[a.fuel_type])+added,3); d.inventory=inv; a.actual_tick=inst.tick; a.status="ARRIVED"; audit(db,"supply.arrived","supply",a.id,metadata={"added":added})

def process_departures(db: Session,inst:SimulationInstance):
    for a in db.query(Allocation).filter(Allocation.status=="PENDING", Allocation.created_tick<=inst.tick).all():
        route=db.get(Route,a.route_id)
        if route.status!="AVAILABLE": a.status="FAILED"; a.failure_reason="ROUTE_UNAVAILABLE"; continue
        a.status="IN_TRANSIT"; a.departure_tick=inst.tick; a.expected_arrival_tick=inst.tick+route.transit_ticks; audit(db,"allocation.departed","allocation",a.id)

def process_arrivals(db: Session,inst:SimulationInstance):
    for a in db.query(Allocation).filter(Allocation.status=="IN_TRANSIT", Allocation.expected_arrival_tick<=inst.tick).all():
        s=db.get(Station,a.destination_station_id); inv=dict(s.inventory); cap=float(s.capacity[a.fuel_type]); room=max(0,cap-float(inv[a.fuel_type])); received=min(room,float(a.quantity))
        inv[a.fuel_type]=round(float(inv[a.fuel_type])+received,3); s.inventory=inv; a.actual_arrival_tick=inst.tick; a.status="ARRIVED"; audit(db,"allocation.arrived","allocation",a.id,metadata={"received":received})

def generate_demand(db:Session,inst:SimulationInstance,scenario:dict):
    profiles=scenario["demand_profiles"]
    regions={r.id:r for r in db.query(Region).all()}
    for s in db.query(Station).all():
        profile=profiles[s.demand_profile]
        inv=dict(s.inventory)
        for fuel in FUEL_TYPES:
            daily=float(profile["daily_liters"][fuel]); base=daily/96.0
            factor=_hour_factor(inst.sim_time.hour,s.demand_profile)*float(regions[s.region_id].demand_factor)*float(s.demand_multiplier)
            noise=float(profile.get("noise",0.1)); rng=_rng(inst.seed,inst.tick,s.id,f"demand:{fuel}"); jitter=1+rng.uniform(-noise,noise)
            demand=max(0,base*factor*jitter)
            if s.status=="OUTAGE": served=0.0
            else: served=min(float(inv[fuel]),demand)
            unmet=demand-served
            inv[fuel]=round(max(0,float(inv[fuel])-served),3)
            db.add(DemandObservation(station_id=s.id,fuel_type=fuel,tick=inst.tick,sim_time=inst.sim_time,demand_liters=round(demand,3),served_liters=round(served,3),unmet_liters=round(unmet,3)))
        s.inventory=inv

def tick_once(db:Session,scenario:dict):
    """Advance the simulation by exactly one tick.

    A tick is the simulator's discrete unit of time progression. Each tick:
      1. Runs the deterministic 6-step engine pipeline (events, supply,
         departures, arrivals, demand, event resolution).
      2. Records an audit log entry.
      3. Increments `inst.tick` by 1 and advances `inst.sim_time` by
         `inst.tick_minutes` simulated minutes (default 15, override via the
         TICK_MINUTES env var).

    Called by the background runner at a rate controlled by SIMULATION_SPEED
    (ticks/sec wall-clock) and by `POST /admin/step` for deterministic advance.
    """
    inst=db.get(SimulationInstance,1)
    if not inst: raise RuntimeError("Simulation not initialized")
    start_events(db,inst)
    process_supply(db,inst)
    process_departures(db,inst)
    process_arrivals(db,inst)
    generate_demand(db,inst,scenario)
    resolve_events(db,inst)
    audit(db,"simulation.tick","instance",1,metadata={"tick":inst.tick})
    inst.tick += 1
    inst.sim_time = inst.sim_time + timedelta(minutes=inst.tick_minutes)
    db.commit()
    return {"tick":inst.tick,"sim_time":inst.sim_time.isoformat()}
