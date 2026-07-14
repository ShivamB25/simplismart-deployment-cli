from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone


_TIME_FORMATS = ("%H:%M", "%H", "%I:%M%p", "%I%p")


def parse_clock(value: str) -> time:
    """Parse common 12-hour or 24-hour clock input."""
    normalized = value.strip().upper().replace(" ", "")
    for format_string in _TIME_FORMATS:
        try:
            return datetime.strptime(normalized, format_string).time()
        except ValueError:
            continue
    raise ValueError(
        f"invalid time {value!r}; use values such as 10:00, 10am, 01:00, or 1:00am"
    )


def local_timezone_name() -> str:
    """Return the system's local IANA timezone name."""
    timezone = get_localzone()
    name = getattr(timezone, "key", None) or str(timezone)
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            "could not determine an IANA system timezone; pass --timezone explicitly"
        ) from exc
    return name


@dataclass(frozen=True)
class DailyWindow:
    on_at: time
    off_at: time
    timezone_name: str

    @classmethod
    def create(
        cls,
        *,
        on_at: str,
        off_at: str,
        timezone_name: str | None = None,
    ) -> DailyWindow:
        start = parse_clock(on_at)
        stop = parse_clock(off_at)
        if start == stop:
            raise ValueError("--on-at and --off-at must be different")

        resolved_timezone = timezone_name or local_timezone_name()
        try:
            ZoneInfo(resolved_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"unknown IANA timezone {resolved_timezone!r}"
            ) from exc
        return cls(
            on_at=start,
            off_at=stop,
            timezone_name=resolved_timezone,
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def crosses_midnight(self) -> bool:
        return self.off_at < self.on_at

    @property
    def on_cron(self) -> str:
        return f"{self.on_at.minute} {self.on_at.hour} * * *"

    @property
    def off_cron(self) -> str:
        return f"{self.off_at.minute} {self.off_at.hour} * * *"

    def is_active(self, at: datetime | None = None) -> bool:
        """Use concrete instants; ambiguous times use fold 0 and off wins ties."""
        moment = at or datetime.now(self.timezone)
        if moment.tzinfo is None:
            raise ValueError("schedule evaluation requires a timezone-aware datetime")
        local = moment.astimezone(self.timezone)
        timestamp = local.timestamp()
        boundaries: list[tuple[float, str]] = []
        for day_offset in (-1, 0):
            day = local.date() + timedelta(days=day_offset)
            boundaries.extend(
                (
                    (self._normalized_boundary(day, self.on_at).timestamp(), "on"),
                    (self._normalized_boundary(day, self.off_at).timestamp(), "off"),
                )
            )
        _, latest_kind = max(
            (boundary for boundary in boundaries if boundary[0] <= timestamp),
            key=lambda boundary: (boundary[0], boundary[1] == "off"),
        )
        return latest_kind == "on"

    def next_boundary(self, after: datetime | None = None) -> datetime:
        """Return the next boundary; nonexistent DST times shift forward."""
        moment = after or datetime.now(self.timezone)
        if moment.tzinfo is None:
            raise ValueError("boundary calculation requires a timezone-aware datetime")
        local = moment.astimezone(self.timezone)
        candidates: list[datetime] = []
        for day_offset in range(3):
            day = local.date() + timedelta(days=day_offset)
            candidates.extend(
                (
                    self._normalized_boundary(day, self.on_at),
                    self._normalized_boundary(day, self.off_at),
                )
            )
        future = (
            candidate
            for candidate in candidates
            if candidate.timestamp() > local.timestamp()
        )
        return min(future, key=datetime.timestamp)

    def _normalized_boundary(self, day: date, clock: time) -> datetime:
        candidate = datetime.combine(day, clock, self.timezone)
        return candidate.astimezone(UTC).astimezone(self.timezone)

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "on_at": self.on_at.strftime("%H:%M"),
            "off_at": self.off_at.strftime("%H:%M"),
            "timezone": self.timezone_name,
            "crosses_midnight": self.crosses_midnight,
        }
