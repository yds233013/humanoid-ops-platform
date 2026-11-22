"""v2 01D: the workspace time zone is the ONE display zone (platform_core.timezones).

Screens, labels and Today's day boundary follow the workspace row's time zone (settings.TIME_ZONE only when no
row exists). Storage and audit timestamps stay UTC. DST: America/Los_Angeles springs forward on 2026-03-08
(02:00 -> 03:00) and falls back on 2026-11-01 (01:00-02:00 happens twice). SYNTHETIC data only.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from operations import today
from platform_core import timezones
from platform_core import workspace as ws_commands
from platform_core.models import AuditEvent, Workspace
from platform_core.templatetags import ui
from tests.conftest import make_user

pytestmark = pytest.mark.django_db

LA, NY = "America/Los_Angeles", "America/New_York"
Y = r"(?: 2026)?"  # local_dt adds the year only when it is not the current year


def set_zone(name: str) -> None:
    Workspace.ensure()
    Workspace.objects.filter(pk=1).update(time_zone=name)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ------------------------------------------------------------------ the source of truth
def test_display_zone_is_the_workspace_row_with_settings_as_fallback():
    set_zone(NY)
    assert timezones.workspace_zone_name() == NY
    assert ui.workspace_zone().key == NY
    set_zone(LA)
    assert timezones.workspace_zone_name() == LA
    Workspace.objects.filter(pk=1).update(time_zone="Mars/Olympus")  # unusable value: never crash a page
    assert timezones.workspace_zone_name() == settings.TIME_ZONE
    Workspace.objects.all().delete()  # fresh database: no row yet
    assert timezones.workspace_zone_name() == settings.TIME_ZONE == LA


def test_new_york_and_los_angeles_show_different_times_and_labels():
    instant = utc(2026, 12, 8, 18, 0)
    set_zone(NY)
    with timezones.display_zone():
        assert re.fullmatch(rf"Tue 8 Dec{Y}, 13:00 EST", ui.local_dt(instant))
        assert ui.local_time(instant) == "13:00"
        assert ui.workspace_tz_label().startswith(f"{NY} (E")
        assert timezone.get_current_timezone_name() == NY
    set_zone(LA)
    with timezones.display_zone():
        assert re.fullmatch(rf"Tue 8 Dec{Y}, 10:00 PST", ui.local_dt(instant))
        assert ui.workspace_tz_label().startswith(f"{LA} (P")
        # sentences produced by the read models are rewritten into the same zone
        assert "10:00 PST" in ui.localize_utc_text("Due 2026-12-08 18:00 UTC")
        assert "10:00 PST" in ui.localize_utc_text("Due 2026-12-08T18:00:00+00:00")


def test_pages_follow_the_workspace_zone_per_request():
    from tests.planning_world import DAY, build_planning, new_visit

    w = build_planning(robots=1)
    code = new_visit(w, d=DAY, h0=10, h1=11)  # 10:00-11:00 in Los Angeles = 18:00-19:00 UTC
    c = Client()
    c.force_login(w.ops)
    set_zone(LA)
    week = c.get(f"/planning/?week={DAY.isoformat()}").content.decode()
    assert f"Times are in {LA} (P" in week and "10:00 to 11:00" in week
    detail = c.get(f"/planning/visits/{code}/").content.decode()
    assert re.search(rf"On site Tue 8 Dec{Y}, 10:00 PST to 11:00", detail)
    set_zone(NY)
    week = c.get(f"/planning/?week={DAY.isoformat()}").content.decode()
    assert f"Times are in {NY} (E" in week and "13:00 to 14:00" in week
    detail = c.get(f"/planning/visits/{code}/").content.decode()
    assert re.search(rf"On site Tue 8 Dec{Y}, 13:00 EST to 14:00", detail)
    assert "UTC" not in re.sub(r"<details.*?</details>", "", detail, flags=re.S).split("<main>")[-1][:2000]
    # the zone is read once per request, not once per rendered time
    with CaptureQueriesContext(connection) as q:
        c.get(f"/planning/?week={DAY.isoformat()}")
    reads = [x for x in q.captured_queries if '"platform_core_workspace"."time_zone"' in x["sql"]]
    assert len(reads) == 1, reads


# ------------------------------------------------------------------ DST
def test_fall_back_hour_shows_both_offsets_in_los_angeles():
    set_zone(LA)
    with timezones.display_zone():
        first = ui.local_dt(utc(2026, 11, 1, 8, 30))  # 01:30 PDT (UTC-7)
        second = ui.local_dt(utc(2026, 11, 1, 9, 30))  # 01:30 PST (UTC-8), one hour later
        assert re.fullmatch(rf"Sun 1 Nov{Y}, 01:30 PDT", first), first
        assert re.fullmatch(rf"Sun 1 Nov{Y}, 01:30 PST", second), second
        assert ui.local_date(utc(2026, 11, 1, 9, 30)).startswith("Sun 1 Nov")


def test_spring_gap_jumps_from_0159_pst_to_0300_pdt():
    set_zone(LA)
    with timezones.display_zone():
        assert re.fullmatch(rf"Sun 8 Mar{Y}, 01:59 PST", ui.local_dt(utc(2026, 3, 8, 9, 59)))
        assert re.fullmatch(rf"Sun 8 Mar{Y}, 03:00 PDT", ui.local_dt(utc(2026, 3, 8, 10, 0)))
        assert ui.local_time(utc(2026, 3, 8, 10, 0)) == "03:00"  # 02:xx never appears


def test_dst_in_new_york_uses_new_york_rules():
    set_zone(NY)
    with timezones.display_zone():
        assert ui.local_dt(utc(2026, 11, 1, 5, 30)).endswith("01:30 EDT")
        assert ui.local_dt(utc(2026, 11, 1, 6, 30)).endswith("01:30 EST")
        assert ui.local_dt(utc(2026, 3, 8, 7, 0)).endswith("03:00 EDT")


# ------------------------------------------------------------------ Today's day boundary
def test_today_boundary_follows_the_workspace_zone_near_midnight(admin_user_for_today):
    at = utc(2026, 12, 9, 6, 30)  # Los Angeles: Tue 8 Dec 22:30; New York: Wed 9 Dec 01:30
    set_zone(LA)
    with timezones.display_zone():
        assert today.local_day(at).isoformat() == "2026-12-08"
        assert today.build_today(at, admin_user_for_today).local_date_text == "Tue 8 Dec"
        la_window = today.window_minutes(at, today.local_day(at))
    set_zone(NY)
    with timezones.display_zone():
        assert today.local_day(at).isoformat() == "2026-12-09"
        assert today.build_today(at, admin_user_for_today).local_date_text == "Wed 9 Dec"
        ny_window = today.window_minutes(at, today.local_day(at))
    # the window ends at the end of the workspace's tomorrow: 25.5 h (LA) vs 46.5 h (NY) from 06:30 UTC
    assert la_window == int(timedelta(hours=25, minutes=30).total_seconds() // 60) + 1
    assert ny_window == int(timedelta(hours=46, minutes=30).total_seconds() // 60) + 1


@pytest.fixture
def admin_user_for_today(db):
    return make_user("t_today_ops", "ops_lead")


def test_planner_this_week_follows_the_workspace_zone_near_midnight(monkeypatch):
    # Mon 7 Dec 2026 06:30 UTC: still Sunday 6 Dec in Los Angeles, already Monday 7 Dec in New York
    monkeypatch.setattr(timezone, "now", lambda: utc(2026, 12, 7, 6, 30))
    c = Client()
    c.force_login(make_user("t_week_ops", "ops_lead"))
    set_zone(LA)
    assert "Week of Mon 30 Nov 2026" in c.get("/planning/").content.decode()
    set_zone(NY)
    assert "Week of Mon 7 Dec 2026" in c.get("/planning/").content.decode()


# ------------------------------------------------------------------ storage stays UTC
def test_stored_and_audit_timestamps_stay_utc_whatever_the_display_zone():
    admin = make_user("t_tz_admin", "admin")
    set_zone(NY)
    before = timezone.now()
    with timezones.display_zone("Asia/Tokyo"):
        ws_commands.update_workspace(admin, name="Northbay", region="Bay Area", time_zone=LA)
    after = timezone.now()
    row = AuditEvent.objects.get(command="platform_core.update_workspace")
    assert row.occurred_at.utcoffset() == timedelta(0)  # aware, UTC, as stored
    assert before - timedelta(seconds=5) <= row.occurred_at <= after + timedelta(seconds=5)
    with connection.cursor() as cur:
        cur.execute("SHOW TIME ZONE")
        assert cur.fetchone()[0] == "UTC"  # the database session never takes the display zone
        cur.execute(
            "SELECT to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') FROM platform_core_auditevent"
            " WHERE id = %s",
            [row.pk],
        )
        raw = cur.fetchone()[0]
    assert raw == row.occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    assert row.payload["time_zone"] == LA and Workspace.objects.get(pk=1).time_zone == LA
