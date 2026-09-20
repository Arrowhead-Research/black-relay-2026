#!/usr/bin/python3
"""Fail if the newest restic snapshot is older than the given hour limit."""
import datetime
import json
import sys


def main():
    limit_hours = int(sys.argv[1])
    snapshots = json.load(sys.stdin)
    if not snapshots:
        print("no snapshots are present in the repository", file=sys.stderr)
        return 1
    newest = max(
        datetime.datetime.fromisoformat(s["time"].replace("Z", "+00:00"))
        for s in snapshots
    )
    age = datetime.datetime.now(datetime.timezone.utc) - newest
    if age.total_seconds() > limit_hours * 3600:
        print(
            "newest snapshot is %d hours old" % int(age.total_seconds() // 3600),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
