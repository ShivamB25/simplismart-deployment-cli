from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from simplismart_deployment_cli import scheduling
from simplismart_deployment_cli.scheduling import DailyWindow, parse_clock


def test_parse_clock_accepts_human_12_and_24_hour_values() -> None:
    assert parse_clock("10am").strftime("%H:%M") == "10:00"
    assert parse_clock("1:13 am").strftime("%H:%M") == "01:13"
    assert parse_clock("22:45").strftime("%H:%M") == "22:45"


def test_overnight_window_stays_active_across_midnight() -> None:
    window = DailyWindow.create(
        on_at="10:00",
        off_at="01:00",
        timezone_name="Asia/Kolkata",
    )
    timezone = ZoneInfo("Asia/Kolkata")

    assert window.crosses_midnight is True
    assert window.is_active(datetime(2026, 7, 14, 23, 0, tzinfo=timezone)) is True
    assert window.is_active(datetime(2026, 7, 15, 0, 59, tzinfo=timezone)) is True
    assert window.is_active(datetime(2026, 7, 15, 1, 0, tzinfo=timezone)) is False
    assert window.is_active(datetime(2026, 7, 15, 9, 59, tzinfo=timezone)) is False


def test_daily_window_generates_native_cron_expressions() -> None:
    window = DailyWindow.create(
        on_at="10am",
        off_at="1am",
        timezone_name="Asia/Kolkata",
    )

    assert window.on_cron == "0 10 * * *"
    assert window.off_cron == "0 1 * * *"
    assert window.next_boundary(
        datetime(2026, 7, 14, 23, 0, tzinfo=window.timezone)
    ) == datetime(2026, 7, 15, 1, 0, tzinfo=window.timezone)


def test_nonexistent_dst_boundary_shifts_forward() -> None:
    window = DailyWindow.create(
        on_at="02:30",
        off_at="04:00",
        timezone_name="America/New_York",
    )

    boundary = window.next_boundary(
        datetime(2026, 3, 8, 1, 0, tzinfo=window.timezone)
    )

    assert boundary.isoformat() == "2026-03-08T03:30:00-04:00"


def test_ambiguous_dst_boundary_uses_first_occurrence_for_catch_up() -> None:
    window = DailyWindow.create(
        on_at="22:00",
        off_at="01:30",
        timezone_name="America/New_York",
    )

    first_1_15 = datetime(2026, 11, 1, 1, 15, tzinfo=window.timezone, fold=0)
    second_1_15 = datetime(2026, 11, 1, 1, 15, tzinfo=window.timezone, fold=1)

    assert window.is_active(first_1_15) is True
    assert window.is_active(second_1_15) is False
    assert window.next_boundary(second_1_15).date().isoformat() == "2026-11-01"
    assert window.next_boundary(second_1_15).hour == 22


def test_spring_forward_collision_prefers_off_boundary() -> None:
    window = DailyWindow.create(
        on_at="02:00",
        off_at="03:00",
        timezone_name="America/New_York",
    )

    after_collision = datetime(2026, 3, 8, 3, 1, tzinfo=window.timezone)

    assert window.is_active(after_collision) is False


def test_daily_window_defaults_to_system_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduling, "get_localzone", lambda: ZoneInfo("Asia/Kolkata"))

    window = DailyWindow.create(on_at="10am", off_at="1am")

    assert window.timezone_name == "Asia/Kolkata"


def test_equal_window_boundaries_are_rejected() -> None:
    with pytest.raises(ValueError, match="must be different"):
        DailyWindow.create(
            on_at="10:00",
            off_at="10am",
            timezone_name="UTC",
        )
