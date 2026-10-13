"""
Test suite verifying attendance calculation business rules (R1 - R5).
Runs in-memory without requiring a live database connection.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# Ensure project root is in Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import (
    IST,
    truncate_to_seconds,
    ms_to_datetime,
    datetime_to_ms,
    get_shift_schedule,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
)


def test_r1_instant_truncation():
    """R1: Timestamps must be truncated to whole seconds."""
    raw_ms = 1767673800999  # contains 999 sub-second ms
    truncated_dt = ms_to_datetime(raw_ms)
    expected_ms = 1767673800000
    actual_ms = datetime_to_ms(truncated_dt)

    assert actual_ms == expected_ms
    assert truncated_dt.microsecond == 0


def test_r1_normal_shift_schedule():
    """R1: Normal daytime shift window and attendance date."""
    dt_normal = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    att_date, start_dt, end_dt = get_shift_schedule(dt_normal, "09:30", "18:30")

    assert att_date == "2026-07-06"
    assert start_dt == datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    assert end_dt == datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)


def test_r1_overnight_shift_schedule():
    """R1: Overnight shift boundaries and midnight-crossing date attribution."""
    # Punch before midnight (21:55 IST)
    dt_before = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    att_date_ob, start_dt_ob, end_dt_ob = get_shift_schedule(dt_before, "22:00", "06:00")
    assert att_date_ob == "2026-07-06"
    assert start_dt_ob == datetime(2026, 7, 6, 22, 0, 0, tzinfo=IST)
    assert end_dt_ob == datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)

    # Punch after midnight but before shift end (05:40 IST next day)
    dt_after = datetime(2026, 7, 7, 5, 40, 0, tzinfo=IST)
    att_date_oa, start_dt_oa, end_dt_oa = get_shift_schedule(dt_after, "22:00", "06:00")
    assert att_date_oa == "2026-07-06"
    assert start_dt_oa == datetime(2026, 7, 6, 22, 0, 0, tzinfo=IST)
    assert end_dt_oa == datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)


def test_r2_late_arrival():
    """R2: 10-minute grace period with floor whole minutes from shift start."""
    # 09:40:00 -> within grace period (10m 00s), late_minutes = 0
    t1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(t1, "09:30", "18:30") == 0

    # 09:40:01 -> strictly > 10 min, late_minutes = 10
    t2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(t2, "09:30", "18:30") == 10

    # 10:15:59 -> 45 minutes late from shift start
    t3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(t3, "09:30", "18:30") == 45


def test_r3_overtime():
    """R3: Overtime is counted only when duration after shift end is >= 30 min."""
    # Punch-out before shift end (18:25) -> 0
    assert compute_overtime(datetime(2026, 7, 6, 18, 25, 0, tzinfo=IST), "09:30", "18:30", "2026-07-06") == 0

    # 29 minutes after shift end (18:59) -> threshold not met, overtime = 0
    assert compute_overtime(datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST), "09:30", "18:30", "2026-07-06") == 0

    # Exactly 30 minutes after shift end (19:00) -> overtime = 30
    assert compute_overtime(datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST), "09:30", "18:30", "2026-07-06") == 30

    # 45 minutes after shift end (19:15) -> overtime = 45
    assert compute_overtime(datetime(2026, 7, 6, 19, 15, 0, tzinfo=IST), "09:30", "18:30", "2026-07-06") == 45


def test_r4_work_hours_round_half_up():
    """R4: Work hours computed to 2 decimal places using round-half-up."""
    t_in = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)

    # 8h 30m = 8.50
    t_out1 = datetime(2026, 7, 6, 18, 0, 0, tzinfo=IST)
    assert compute_work_hours(t_in, t_out1) == 8.50

    # 8h 15m 30s = 8.25833... -> 8.26
    t_out2 = datetime(2026, 7, 6, 17, 45, 30, tzinfo=IST)
    assert compute_work_hours(t_in, t_out2) == 8.26

    # Tie-breaking half-up check: 18 seconds = 0.005 hours -> rounds up to 0.01
    t_out3 = datetime(2026, 7, 6, 9, 30, 18, tzinfo=IST)
    assert compute_work_hours(t_in, t_out3) == 0.01


def test_r5_half_day_threshold():
    """R5: Half-day true when rounded work_hours < 4.50."""
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.50) is False


if __name__ == "__main__":
    test_r1_instant_truncation()
    test_r1_normal_shift_schedule()
    test_r1_overnight_shift_schedule()
    test_r2_late_arrival()
    test_r3_overtime()
    test_r4_work_hours_round_half_up()
    test_r5_half_day_threshold()
    print("All unit tests in test_rules.py passed successfully!")
