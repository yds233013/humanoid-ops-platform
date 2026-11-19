"""Slice 5 performance (EM perf instruction 3): one supporting index, chosen from EXPLAIN on the scale fixture.

The readiness evaluator (readiness.evaluator -> planning.services.live_feasibility) checks each planned interval
for a clash with ``Reservation.objects.filter(resource=..., released_at__isnull=True, starts_at__lt=...,
ends_at__gt=...).exclude(revision__visit=...).first()``, i.e. ``... ORDER BY planning_reservation.id LIMIT 1``.
On the 200-robot scale fixture (18,079 reservations) PostgreSQL answered it with
``Index Scan using planning_reservation_pkey ... Rows Removed by Filter: 18079`` (about 3 ms, 110 times per
inbox page: ~440 ms of the ~1.2 s evaluator time). An index on (resource_id, id) over the unreleased rows gives
that query the resource's own rows already in id order, so the scan stops at the first clash.

Index only: no view, function, trigger, constraint, data or privilege changes; the query, its result and the
readiness semantics are unchanged (the exclusion constraint of planning/0002 stays the final guard).
"""

from django.db import migrations

INDEX_SQL = r"""
CREATE INDEX IF NOT EXISTS operations_reservation_resource_active ON planning_reservation (resource_id, id)
  WHERE released_at IS NULL;
"""

INDEX_REVERSE_SQL = r"""
DROP INDEX IF EXISTS operations_reservation_resource_active;
"""


class Migration(migrations.Migration):
    dependencies = [("operations", "0001_read_models")]

    operations = [migrations.RunSQL(INDEX_SQL, reverse_sql=INDEX_REVERSE_SQL)]
