from __future__ import annotations

from datetime import date, timedelta

from canvas_task_sync.models import WeekSelection


def monday_for(value: date) -> date:
    return value - timedelta(days=value.weekday())


def selected_week_start(today: date, selection: WeekSelection) -> date:
    offset = {
        WeekSelection.PREVIOUS: -7,
        WeekSelection.CURRENT: 0,
        WeekSelection.NEXT: 7,
    }[selection]
    return monday_for(today) + timedelta(days=offset)
