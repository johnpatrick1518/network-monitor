"""Validated API payloads and documented response types."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DevicePatch(BaseModel):
    """Editable inventory fields; network identity is not client-controlled."""

    friendly_name: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = Field(default=None, max_length=20)
    trusted: bool | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str] | None:
        """Trim, deduplicate, and limit individual tag lengths."""
        if value is None:
            return None
        tags = sorted({tag.strip().lower() for tag in value if tag.strip()})
        if any(len(tag) > 40 for tag in tags):
            raise ValueError("Each tag must be at most 40 characters.")
        return tags

    @field_validator("friendly_name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        """Normalize an empty friendly name to null."""
        return (value.strip() or None) if value else None


class DeviceOut(BaseModel):
    """Device state and ping-based uptime percentages."""

    id: int
    ip: str
    mac: str | None
    hostname: str | None
    vendor: str | None
    friendly_name: str | None
    tags: list[str]
    trusted: bool
    status: Literal["unknown", "up", "down"]
    latency_ms: float | None
    first_seen: str
    last_seen: str
    last_checked: str | None
    consecutive_failures: int
    uptime_24h: float | None
    uptime_7d: float | None
    uptime_30d: float | None


class SampleOut(BaseModel):
    """One raw ping measurement."""

    timestamp: str
    is_up: bool
    latency_ms: float | None


class HistoryOut(BaseModel):
    """Bounded chart history; large series are bucketed without hiding outages."""

    device_id: int
    range: str
    bucket_seconds: int
    samples: list[SampleOut]


class EventOut(BaseModel):
    """A dashboard event."""

    id: int
    device_id: int | None
    kind: str
    message: str
    created_at: str
    notification_status: str


class ScanOut(BaseModel):
    """A background scan request acknowledgement."""

    status: str
    message: str


class SummaryOut(BaseModel):
    """Dashboard totals and background service status."""

    total: int
    demo_mode: bool = False
    online: int
    offline: int
    pending: int
    unknown: int
    average_latency_ms: float | None
    subnet: str | None
    wifi_ssid: str | None = None
    network_interface: str | None = None
    wifi_signal_percent: int | None = None
    scan_running: bool
    last_scan_at: str | None
    last_scan_error: str | None
    scheduler_running: bool


class AlertTestOut(BaseModel):
    """Test delivery result, including per-channel diagnostics."""

    status: str
    channels: dict[str, str]
