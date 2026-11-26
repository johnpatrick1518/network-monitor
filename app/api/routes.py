"""Inventory, monitoring, event, and export endpoints."""

import csv
import io
import ipaddress
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.orm import Session

from app.database import get_db, iso_utc, utcnow
from app.models import Device, Event, HealthSample
from app.monitor import uptime_by_device
from app.scheduler import MonitorService
from app.schemas import AlertTestOut, DeviceOut, DevicePatch, EventOut, HistoryOut, ScanOut, SummaryOut

router = APIRouter(prefix="/api", tags=["Network monitor"])
Database = Annotated[Session, Depends(get_db)]


def get_service(request: Request) -> MonitorService:
    """Retrieve the lifespan-owned background service."""
    return request.app.state.service


Service = Annotated[MonitorService, Depends(get_service)]


def require_device(session: Session, device_id: int) -> Device:
    """Find a device or return a documented 404 response."""
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device


def serialize_device(device: Device, uptimes: dict[str, float | None]) -> dict[str, object]:
    """Serialize inventory with UTC timestamps and computed uptime windows."""
    return {
        "id": device.id, "ip": device.ip, "mac": device.mac,
        "hostname": device.hostname, "vendor": device.vendor,
        "friendly_name": device.friendly_name, "tags": device.tags,
        "trusted": device.trusted, "status": device.status,
        "latency_ms": device.latency_ms, "first_seen": iso_utc(device.first_seen),
        "last_seen": iso_utc(device.last_seen), "last_checked": iso_utc(device.last_checked),
        "consecutive_failures": device.consecutive_failures,
        "uptime_24h": uptimes.get("24h"), "uptime_7d": uptimes.get("7d"),
        "uptime_30d": uptimes.get("30d"),
    }


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    db: Database,
    search: str = "",
    status: Literal["up", "down", "unknown"] | None = None,
    tag: str | None = None,
    trusted: bool | None = None,
    sort: Literal["name", "ip", "mac", "vendor", "status", "latency", "uptime", "last_seen"] = "ip",
    descending: bool = False,
) -> list[dict[str, object]]:
    """List active identities with search, filters, sorting, and all uptime windows."""
    devices = list(db.scalars(select(Device).where(Device.active.is_(True))))
    query = search.casefold().strip()
    if query:
        devices = [device for device in devices if query in " ".join(
            str(value or "") for value in (
                device.friendly_name, device.hostname, device.ip, device.mac, device.vendor, *device.tags
            )
        ).casefold()]
    if status:
        devices = [device for device in devices if device.status == status]
    if tag:
        devices = [device for device in devices if tag.casefold() in device.tags]
    if trusted is not None:
        devices = [device for device in devices if device.trusted == trusted]
    uptimes = uptime_by_device(db, utcnow(), [device.id for device in devices])
    rows = [serialize_device(device, uptimes.get(device.id, {})) for device in devices]

    def sort_value(row: dict[str, object]) -> tuple[bool, object]:
        """Produce comparable sort values with an explicit missing-value flag."""
        if sort == "ip":
            value = int(ipaddress.ip_address(str(row["ip"])))
        elif sort == "name":
            value = str(row["friendly_name"] or row["hostname"] or row["ip"]).casefold()
        elif sort == "latency":
            value = row["latency_ms"]
        elif sort == "uptime":
            value = row["uptime_24h"]
        else:
            value = row[sort]
        return value is None, value if value is not None else ""

    rows.sort(key=sort_value, reverse=descending)
    return rows


@router.get("/devices/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, db: Database) -> dict[str, object]:
    """Return one device and its observed uptime."""
    device = require_device(db, device_id)
    uptimes = uptime_by_device(db, utcnow(), [device_id])
    return serialize_device(device, uptimes.get(device_id, {}))


@router.patch("/devices/{device_id}", response_model=DeviceOut)
def update_device(device_id: int, payload: DevicePatch, db: Database) -> dict[str, object]:
    """Edit a friendly name, tags, or trust classification."""
    device = require_device(db, device_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if value is None and field != "friendly_name":
            raise HTTPException(status_code=422, detail=f"{field} cannot be null.")
        setattr(device, field, value)
    db.commit()
    return serialize_device(device, uptime_by_device(db, utcnow(), [device_id]).get(device_id, {}))


@router.get("/devices/{device_id}/history", response_model=HistoryOut)
def get_history(
    device_id: int,
    db: Database,
    range_: Annotated[Literal["1h", "24h", "7d"], Query(alias="range")] = "24h",
) -> dict[str, object]:
    """Return at most roughly 2,016 points; a mixed bucket is marked down."""
    require_device(db, device_id)
    duration, bucket = {
        "1h": (timedelta(hours=1), 5),
        "24h": (timedelta(days=1), 60),
        "7d": (timedelta(days=7), 300),
    }[range_]
    epoch = cast(func.strftime("%s", HealthSample.timestamp), Integer)
    group = cast(epoch / bucket, Integer)
    query = select(
        func.min(HealthSample.timestamp),
        func.min(case((HealthSample.is_up.is_(True), 1), else_=0)),
        func.avg(HealthSample.latency_ms),
    ).where(
        HealthSample.device_id == device_id,
        HealthSample.timestamp >= utcnow() - duration,
    ).group_by(group).order_by(func.min(HealthSample.timestamp))
    samples = [
        {"timestamp": iso_utc(timestamp), "is_up": bool(is_up), "latency_ms": round(latency, 3) if latency is not None else None}
        for timestamp, is_up, latency in db.execute(query)
    ]
    return {"device_id": device_id, "range": range_, "bucket_seconds": bucket, "samples": samples}


@router.post("/scan", response_model=ScanOut, status_code=202)
def request_scan(service: Service) -> dict[str, str]:
    """Queue private-subnet discovery; never interpret a scan failure as an outage."""
    if not service.request_scan():
        return {"status": "busy", "message": "A scan is already running, or the scheduler is stopping."}
    return {"status": "queued", "message": "Discovery queued. The dashboard will refresh automatically."}


@router.get("/events", response_model=list[EventOut])
def get_events(
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: int | None = None,
    device_id: int | None = None,
) -> list[dict[str, object]]:
    """Return recent events, with optional device filtering and cursor pagination."""
    query = select(Event)
    if before_id is not None:
        query = query.where(Event.id < before_id)
    if device_id is not None:
        query = query.where(Event.device_id == device_id)
    events = db.scalars(query.order_by(Event.id.desc()).limit(limit))
    return [{
        "id": event.id, "device_id": event.device_id, "kind": event.kind,
        "message": event.message, "created_at": iso_utc(event.created_at),
        "notification_status": event.notification_status,
    } for event in events]


@router.get("/summary", response_model=SummaryOut)
def get_summary(db: Database, service: Service) -> dict[str, object]:
    """Return inventory totals, online mean RTT, and discovery diagnostics."""
    devices = list(db.scalars(select(Device).where(Device.active.is_(True))))
    latencies = [device.latency_ms for device in devices if device.status == "up" and device.latency_ms is not None]
    return {
        "total": len(devices), "online": sum(device.status == "up" for device in devices),
        "offline": sum(device.status == "down" for device in devices),
        "pending": sum(device.status == "unknown" for device in devices),
        "unknown": sum(not device.trusted for device in devices),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        **service.snapshot(),
    }


@router.post("/alerts/test", response_model=AlertTestOut)
def test_alert(service: Service) -> dict[str, object]:
    """Send a test notification through configured channels, respecting cooldown."""
    result = service.alerts.notify("test", "Network Monitor: test alert. Your notification channel is working.")
    return {"status": result.pop("status"), "channels": result}


def csv_safe(value: object) -> object:
    """Neutralize spreadsheet formulas from discovered or operator-supplied text."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


@router.get("/export/devices.csv", response_class=Response)
def export_devices(db: Database) -> Response:
    """Export the active inventory as UTF-8 CSV with formula-injection protection."""
    devices = list(db.scalars(select(Device).where(Device.active.is_(True)).order_by(Device.id)))
    uptimes = uptime_by_device(db, utcnow())
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = ["id", "friendly_name", "hostname", "ip", "mac", "vendor", "tags", "trusted", "status", "latency_ms", "uptime_24h", "uptime_7d", "uptime_30d", "first_seen", "last_seen"]
    writer.writerow(fields)
    for device in devices:
        row = serialize_device(device, uptimes.get(device.id, {}))
        row["tags"] = ", ".join(device.tags)
        writer.writerow([csv_safe(row[field]) for field in fields])
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="devices.csv"'},
    )
