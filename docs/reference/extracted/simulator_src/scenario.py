from pathlib import Path
from datetime import datetime
import yaml
from sqlalchemy.orm import Session
from .models import SimulationInstance, Region, Depot, Station, Route, SupplyArrival, DemandObservation, Allocation, DomainEvent, Fault, AuditLog

FUEL_TYPES = {"DIESEL", "PETROL", "OCTANE"}

def load_yaml(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    if data.get("inherit"):
        raise ValueError("Scenario inheritance is intentionally unsupported; make each judging scenario explicit.")
    validate_scenario(data)
    return data

def validate_scenario(s: dict):
    required = ["scenario_id","version","seed","start_time","regions","depots","stations","routes","demand_profiles"]
    missing=[x for x in required if x not in s]
    if missing: raise ValueError(f"Missing scenario keys: {missing}")
    ids=set()
    for kind in ("regions","depots","stations","routes"):
        for item in s[kind]:
            if item["id"] in ids: raise ValueError(f"Duplicate id: {item['id']}")
            ids.add(item["id"])
    region_ids={r["id"] for r in s["regions"]}
    depot_ids={d["id"] for d in s["depots"]}
    station_ids={x["id"] for x in s["stations"]}
    demand_profile_keys=set((s.get("demand_profiles") or {}).keys())
    for r in s["routes"]:
        if int(r.get("transit_ticks", 0)) <= 0:
            raise ValueError(f"Route {r['id']} has invalid transit_ticks: must be > 0")
        if float(r.get("max_shipment", 0)) <= 0:
            raise ValueError(f"Route {r['id']} has invalid max_shipment: must be > 0")
        if r["source_depot_id"] not in depot_ids:
            raise ValueError(f"Route {r['id']} references unknown source_depot_id: {r['source_depot_id']}")
        if r["destination_station_id"] not in station_ids:
            raise ValueError(f"Route {r['id']} references unknown destination_station_id: {r['destination_station_id']}")
    for d in s["depots"]:
        if int(d.get("dispatch_capacity_per_tick", 0)) <= 0:
            raise ValueError(f"Depot {d['id']} has invalid dispatch_capacity_per_tick: must be > 0")
        if d["region_id"] not in region_ids:
            raise ValueError(f"Depot {d['id']} references unknown region_id: {d['region_id']}")
    for st in s["stations"]:
        if st["demand_profile"] not in demand_profile_keys:
            raise ValueError(f"Station {st['id']} references unknown demand_profile: {st['demand_profile']}")
        if st["region_id"] not in region_ids:
            raise ValueError(f"Station {st['id']} references unknown region_id: {st['region_id']}")
    for item in [*s["depots"], *s["stations"]]:
        for fuel in FUEL_TYPES:
            cap=float(item["capacity"].get(fuel,0)); inv=float(item["initial_inventory"].get(fuel,0))
            if cap <= 0 or inv < 0 or inv > cap:
                raise ValueError(f"Invalid capacity/inventory for {item['id']} {fuel}")

def reset_from_scenario(db: Session, path: str):
    s=load_yaml(path)
    for model in [DemandObservation, Allocation, DomainEvent, Fault, AuditLog, SupplyArrival, Route, Station, Depot, Region, SimulationInstance]:
        db.query(model).delete()
    db.commit()
    # tick_minutes is intentionally NOT read from the scenario YAML. It is an
    # env-only setting (TICK_MINUTES). The startup hook in main.py writes the
    # value from settings.tick_minutes into the instance row after this reset.
    inst=SimulationInstance(id=1, scenario_id=s["scenario_id"], scenario_version=str(s["version"]), seed=int(s["seed"]), sim_time=datetime.fromisoformat(s["start_time"]), tick=0, tick_minutes=15, status="PAUSED")
    db.add(inst)
    for x in s["regions"]: db.add(Region(**x))
    db.flush()
    for x in s["depots"]:
        db.add(Depot(id=x["id"], name=x["name"], region_id=x["region_id"], dispatch_capacity_per_tick=x.get("dispatch_capacity_per_tick",10000), capacity=x["capacity"], inventory=x["initial_inventory"], status="OPEN"))
    db.flush()
    for x in s["stations"]:
        db.add(Station(id=x["id"], name=x["name"], region_id=x["region_id"], demand_profile=x["demand_profile"], capacity=x["capacity"], inventory=x["initial_inventory"], status="OPEN", demand_multiplier=1.0))
    db.flush()
    for x in s["routes"]: db.add(Route(**x, status="AVAILABLE"))
    db.flush()
    for x in s.get("supply_arrivals",[]):
        db.add(SupplyArrival(id=x["id"], depot_id=x["depot_id"], fuel_type=x["fuel_type"], quantity=float(x["quantity"]), planned_tick=int(x["arrival_tick"]), status="SCHEDULED"))
    for e in s.get("preloaded_events",[]):
        db.add(DomainEvent(type=e["type"], start_tick=int(e["start_tick"]), end_tick=int(e["start_tick"]+e["duration_ticks"]), status="SCHEDULED", parameters=e.get("parameters",{})))
    db.commit()
    return s
