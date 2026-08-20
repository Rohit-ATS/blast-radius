from hydra import Hydra
from blast import blast_radius, stats
h = Hydra()
print("stats:", stats(h))
for pkg in ["ms", "debug", "chalk"]:
    r, ms = blast_radius(h, pkg, 4)
    print(f"{pkg}: {r['total']} victims in {ms:.0f}ms  {r['histogram']}")
