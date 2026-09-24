"""A schedule that ended in the past is discarded, not extended from its end.

Carrying a data folder over from another machine (or just not running for
months) leaves a schedule whose last block is long gone.  Extending it one
day at a time from that end never reaches today, and the player would loop
on "Schedule Panic" forever.
"""

import datetime
import logging
import types
from unittest.mock import patch

import pytest


def _schedule(end_time, network_type="loop"):
    from fs42.liquid_schedule import LiquidSchedule

    schedule = LiquidSchedule.__new__(LiquidSchedule)
    schedule._l = logging.getLogger("Liquid")
    schedule.conf = {"network_name": "Old", "network_type": network_type}
    schedule._blocks = [types.SimpleNamespace(end_time=end_time)] if end_time else []
    schedule.calls = []
    schedule._flood = lambda start, end: schedule.calls.append(("flood", start, end))
    schedule._fluid = lambda start, end: schedule.calls.append(("fluid", start, end))
    return schedule


def test_stale_schedule_is_discarded_and_restarted_from_today():
    long_ago = datetime.datetime.now() - datetime.timedelta(days=90)
    schedule = _schedule(long_ago)
    with patch("fs42.liquid_schedule.LiquidAPI.delete_blocks") as delete_blocks:
        schedule.add_days(1)
    delete_blocks.assert_called_once_with(schedule.conf)
    kind, start, end = schedule.calls[0]
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    assert start == today and end == today + datetime.timedelta(days=1)


def test_current_schedule_is_extended_from_its_end():
    later = datetime.datetime.now() + datetime.timedelta(hours=5)
    schedule = _schedule(later)
    with patch("fs42.liquid_schedule.LiquidAPI.delete_blocks") as delete_blocks:
        schedule.add_days(1)
    delete_blocks.assert_not_called()
    assert schedule.calls[0][1] == later


def test_schedule_that_ended_earlier_today_is_kept():
    # Ended at 03:00 this morning: still "today", so the day after it is
    # built on and nothing is thrown away.
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    schedule = _schedule(today + datetime.timedelta(hours=3))
    with patch("fs42.liquid_schedule.LiquidAPI.delete_blocks") as delete_blocks:
        schedule.add_days(1)
    delete_blocks.assert_not_called()
