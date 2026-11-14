"""دانلود پس‌زمینه‌ی همه‌ی روزهای Dukascopy با چند کارگر موازی (کش می‌شود)."""
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from . import data

start, end, workers = sys.argv[1], sys.argv[2], int(sys.argv[3])
syms = sys.argv[4].split(",")
jobs = [(s, d) for s in syms for d in pd.date_range(start, end, freq="D", tz="UTC", inclusive="left")
        if d.weekday() != 5]


def one(job):
    s, d = job
    try:
        data.dukascopy(s, d, d + pd.Timedelta(days=1), pause=0)
        return 1
    except Exception as e:
        print("fail", s, d.date(), e, flush=True)
        return 0


done = 0
with ThreadPoolExecutor(workers) as ex:
    for k, ok in enumerate(ex.map(one, jobs), 1):
        done += ok
        if k % 25 == 0:
            print(f"{k}/{len(jobs)} ok={done}", flush=True)
print(f"finished {done}/{len(jobs)}", flush=True)
