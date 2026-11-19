"""`manage.py capacity_report --assumption base --out build/capacity/`

Writes the founder report (report.md) and one CSV per table, and the generated workbook when
capacity.workbook (Engineer C) exists. Everything is SYNTHETIC: a forecast from stated assumptions, not a
company operating plan. The platform baseline (capacity.baseline, Engineer B) is read inside one read-only
snapshot when that module exists; otherwise the registry values are used and the report says so.
"""

import importlib
import importlib.util
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from capacity import report as R
from capacity.inputs import ASSUMPTIONS

WORKBOOK_MODULE = "capacity.workbook"


class Command(BaseCommand):
    help = "Write the synthetic capacity forecast (report.md + CSVs, and the workbook when available)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--assumption", choices=ASSUMPTIONS, default="base")
        parser.add_argument("--out", default="build/capacity/")
        parser.add_argument("--window-days", type=int, default=R.BASELINE_WINDOW_DAYS)

    def handle(self, *args: Any, **opts: Any) -> None:
        assumption = opts["assumption"]
        out = Path(opts["out"])
        out.mkdir(parents=True, exist_ok=True)
        if R.baseline_available():
            from operations.snapshot import read_snapshot

            with read_snapshot() as at:
                start = at - timedelta(days=opts["window_days"])
                values, baseline = R.load_baseline(assumption, at, start=start, end=at)
            report = R.founder_report(assumption, values=values, baseline=baseline)
        else:
            report = R.founder_report(assumption)
        written = [out / "report.md"]
        written[0].write_text(R.render_markdown(report), encoding="utf-8")
        for name, text in R.render_csv(report).items():
            path = out / name
            path.write_text(text, encoding="utf-8")
            written.append(path)
        if importlib.util.find_spec(WORKBOOK_MODULE) is not None:
            workbook = importlib.import_module(WORKBOOK_MODULE)
            path = out / "capacity_model.xlsx"
            workbook.build_workbook(path)
            written.append(path)
        else:
            self.stdout.write("capacity.workbook not available: workbook skipped")
        self.stdout.write(f"Baseline: {report.baseline.status}")
        for path in written:
            self.stdout.write(f"wrote {path}")
