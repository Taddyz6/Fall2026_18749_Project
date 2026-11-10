"""Merge per-process console logs from several computers into one timeline.

Each demo process appends its console to logs/console/<date>/<process>.log.
Copy those folders from every computer into one place, then for example:

    python scripts/merge_logs.py laptopA/ laptopB/ laptopC/ laptopD/ --since 14:05 --events

Lines are ordered by their local timestamps; laptop clocks usually agree to
within a few milliseconds, so close events on different computers may swap.
"""

import argparse
import re
from pathlib import Path


STAMPED = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\] (.*)$")
ROUTINE_HEARTBEAT = re.compile(r"^\[\d+\] \S+ (sending heartbeat|receives heartbeat)")
TRAFFIC = re.compile(r"^(Sending <|Received <|my_state_|request_num \d+: (Delivered|Discarded)"
                     r"|\S+: i_am_ready = 0; logged <)")


def log_files(paths):
    files = []
    for path in map(Path, paths):
        files.extend(sorted(path.rglob("*.log")) if path.is_dir() else [path])
    return files


def within(timestamp, since, until):
    # A bound with a date compares to the whole timestamp; "HH:MM[:SS]" to the time only.
    def key(bound):
        return timestamp if "-" in bound else timestamp[11:]
    if since and key(since) < since:
        return False
    return not (until and key(until)[:len(until)] > until)


def merge(files, since=None, until=None, pattern=None, heartbeats=False, events_only=False):
    entries = []
    for order, file in enumerate(files):
        stamp = None
        for number, raw in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines()):
            match = STAMPED.match(raw)
            if match:
                stamp, text = match.groups()
            elif stamp is None or not raw.strip():
                continue
            else:
                text = raw  # A continuation line, such as a traceback, keeps the last timestamp.
            if not heartbeats and ROUTINE_HEARTBEAT.match(text):
                continue
            if events_only and TRAFFIC.match(text):
                continue
            if pattern and not pattern.search(text):
                continue
            if within(stamp, since, until):
                entries.append((stamp, order, number, file.stem, text))
    entries.sort()
    width = max((len(entry[3]) for entry in entries), default=0)
    return [f"{stamp}  {name:<{width}} | {text}" for stamp, _, _, name, text in entries]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="log files or folders that contain them")
    parser.add_argument("--since", help='start time, "HH:MM[:SS]" or "YYYY-MM-DD HH:MM[:SS]"')
    parser.add_argument("--until", help="end time, same formats")
    parser.add_argument("--grep", help="only lines matching this regular expression")
    parser.add_argument("--heartbeats", action="store_true", help="keep routine heartbeat lines")
    parser.add_argument("--events", action="store_true",
                        help="hide request/reply traffic; keep membership, readiness, views, checkpoints, errors")
    args = parser.parse_args()
    pattern = re.compile(args.grep) if args.grep else None
    for line in merge(log_files(args.paths), args.since, args.until, pattern, args.heartbeats, args.events):
        print(line)


if __name__ == "__main__":
    main()
