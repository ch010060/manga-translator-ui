"""Per-stage time shares from a GUI log (result/log_*.txt).

Usage: python scripts/stage_timing.py result/log_XXXX.txt
Each stage runs from its "Running xxx" marker to the next marker.
ponytail: only accurate in serial mode (batch_concurrent=false); in concurrent mode the markers interleave across threads.
"""
import re, sys
from collections import defaultdict
from datetime import datetime

MARK = re.compile(r'^(\S+ \S+) - \w+ - \[.*?\] - (Running [\w ]+|No text regions.*)$')

def parse(lines):
    events = []
    for ln in lines:
        m = MARK.match(ln.rstrip())
        if m:
            events.append((datetime.strptime(m[1], '%Y-%m-%d %H:%M:%S,%f'), m[2].removeprefix('Running ')))
    tot, cnt = defaultdict(float), defaultdict(int)
    for (t0, s), (t1, _) in zip(events, events[1:]):
        tot[s] += (t1 - t0).total_seconds(); cnt[s] += 1
    return tot, cnt

if __name__ == '__main__':
    tot, cnt = parse(open(sys.argv[1], encoding='utf-8'))
    all_ = sum(tot.values()) or 1
    for s, v in sorted(tot.items(), key=lambda x: -x[1]):
        print(f'{s:<22}{v:9.1f}s {v/all_:6.1%}  n={cnt[s]}')
