from datetime import datetime, timezone
import asyncio, random
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from .models import Fault

def active_faults(db: Session):
    now=datetime.now(timezone.utc)
    rows=db.query(Fault).filter(Fault.active.is_(True)).all()
    out=[]
    for f in rows:
        end=f.end_wall_time
        if end.tzinfo is None: end=end.replace(tzinfo=timezone.utc)
        if end <= now: f.active=False
        else: out.append(f)
    db.commit(); return out

async def apply_request_faults(db:Session, request:Request):
    for f in active_faults(db):
        p=f.parameters or {}
        if f.type=="latency": await asyncio.sleep(max(0,float(p.get("delay_ms",500)))/1000)
        elif f.type=="unavailable": return JSONResponse(status_code=503,content={"error":{"code":"FAULT_INJECTED","message":"Simulator API temporarily unavailable."}})
        elif f.type=="error_rate":
            if random.random() < float(p.get("rate",0.25)):
                return JSONResponse(status_code=503,content={"error":{"code":"FAULT_INJECTED","message":"Injected transient API error."}})
    return None

def has_active_fault(db: Session, fault_type: str) -> bool:
    """Return True if any active fault of the given type currently exists.

    Expired faults are deactivated as a side-effect.
    """
    return any(f.type == fault_type for f in active_faults(db))
