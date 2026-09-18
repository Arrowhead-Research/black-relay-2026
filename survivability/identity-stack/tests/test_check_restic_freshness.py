"""Call the freshness helper instead of asserting on its source text.

This is the only real function in the repository. It decides whether the weekly
verification job alerts, so its edge cases -- an empty repository, restic's
nanosecond timestamps, non-UTC offsets -- are worth exercising directly.
"""
from pathlib import Path
import datetime
import json
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = (
    ROOT / "ansible" / "roles" / "backup_restic" / "files"
    / "check-restic-freshness.py"
)
STALE_AFTER_HOURS = 26


def snapshot(age_hours, *, fractional="", offset="+00:00"):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=age_hours
    )
    if offset != "+00:00":
        moment = moment.astimezone(
            datetime.timezone(datetime.timedelta(hours=int(offset[:3])))
        )
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S") + fractional + offset
    return {"time": stamp, "id": "0" * 64}


def run(snapshots, limit_hours=STALE_AFTER_HOURS):
    return subprocess.run(
        ["python3", str(HELPER), str(limit_hours)],
        input=json.dumps(snapshots),
        capture_output=True,
        text=True,
    )


class CheckResticFreshnessTests(unittest.TestCase):
    def test_empty_repository_fails(self):
        result = run([])
        self.assertEqual(result.returncode, 1)
        self.assertIn("no snapshots", result.stderr)

    def test_recent_snapshot_passes(self):
        result = run([snapshot(2)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_older_than_the_objective_fails(self):
        result = run([snapshot(STALE_AFTER_HOURS + 1)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("hours old", result.stderr)

    def test_boundary_just_inside_the_objective_passes(self):
        result = run([snapshot(STALE_AFTER_HOURS - 1)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_newest_snapshot_decides_when_older_ones_are_stale(self):
        result = run([snapshot(500), snapshot(1), snapshot(200)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_restic_nanosecond_timestamps_parse(self):
        """restic emits Go RFC3339Nano, which can carry nine fractional digits."""
        result = run([snapshot(1, fractional=".123456789")])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_utc_offsets_are_compared_correctly(self):
        fresh = run([snapshot(1, offset="+02:00")])
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        stale = run([snapshot(STALE_AFTER_HOURS + 5, offset="+02:00")])
        self.assertEqual(stale.returncode, 1)


if __name__ == "__main__":
    unittest.main()
