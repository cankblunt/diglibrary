"""Score the B1 sweep: per arm, the distribution of peak z-scores.

Statistic per file = max over the two channel views of z at each alpha.
Also reports, for every candidate threshold, convictions per arm — the
cdfilter and original rows are the veto.
"""

import sys
from collections import defaultdict

rows = defaultdict(lambda: defaultdict(dict))  # arm -> index -> view -> (z1,z2,z3,tops)
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    lines = handle.read().splitlines()[1:]
for line in lines:
    parts = line.split("\t")
    if parts[0] == "ERR":
        print("ERR row:", line.strip())
        continue
    name, view = parts[0], parts[1]
    idx, arm = name.replace(".flac", "").split("-", 1)
    z = [float(parts[2]), float(parts[4]), float(parts[6])]
    tops = [int(parts[3]), int(parts[5]), int(parts[7])]
    rows[arm][idx][view] = (z, tops)

ARMS = ["original", "cdfilter", "lame128", "lame192", "lame256", "lame320", "lame320-nolp"]
ALPHAS = ["1e-2", "1e-3", "1e-4"]

# per-file statistic: for each alpha, max z across views; plus offset agreement
stats = {arm: {a: [] for a in range(3)} for arm in ARMS}
agree = {arm: 0 for arm in ARMS}
for arm in ARMS:
    for views in rows[arm].values():
        zs = {a: max(v[0][a] for v in views.values()) for a in range(3)}
        for a in range(3):
            stats[arm][a].append(zs[a])
        tops = [t for v in views.values() for t in v[1]]
        if len(set(tops)) == 1:
            agree[arm] += 1


def q(xs, f):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(f * len(xs)))]


for a, alpha in enumerate(ALPHAS):
    print(f"\n== alpha {alpha}: peak z per arm (min / median / max), n per arm ==")
    for arm in ARMS:
        xs = stats[arm][a]
        if not xs:
            print(f"{arm:<13} MISSING")
            continue
        line = f"n={len(xs):>3}  min={min(xs):7.2f}  med={q(xs, 0.5):7.2f}  max={max(xs):7.2f}"
        print(f"{arm:<13} {line}")
    honest = stats["original"][a] + stats["cdfilter"][a]
    ceiling = max(honest)
    print(f"honest ceiling (max over original+cdfilter): {ceiling:.2f}")
    for arm in ARMS:
        caught = sum(1 for z in stats[arm][a] if z > ceiling)
        print(f"  above-honest-ceiling {arm:<13} {caught}/{len(stats[arm][a])}")

print("\n== all six offsets agree (2 views x 3 alphas) ==")
for arm in ARMS:
    print(f"{arm:<13} {agree[arm]}/{len(rows[arm])}")
