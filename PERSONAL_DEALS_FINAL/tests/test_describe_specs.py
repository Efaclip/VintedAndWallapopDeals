"""Regression: rendered alert specs must come from normalized fields,
never the raw category string; GPU chips must not duplicate; GPU memory
is labeled VRAM, not RAM."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from run_pipeline import describe_specs

cases = [
    ("gpu listing: no chip duplication, VRAM label",
     {"category": "gpu", "brand": "NVIDIA", "model": "RTX 4070 SUPER",
      "gpu": "RTX 4070 SUPER", "ram_gb": 12, "processor": None},
     "NVIDIA RTX 4070 SUPER, 12GB VRAM"),
    ("laptop listing: full specs in order",
     {"category": "laptop", "brand": "Apple", "model": "MacBook Air 13",
      "processor": "M2", "ram_gb": 16, "storage_gb": 512,
      "screen_inches": None, "gpu": None, "edition": None},
     "Apple MacBook Air 13, M2, 16GB RAM, 512GB storage"),
    ("nothing identified",
     {"category": "laptop", "brand": None, "model": None, "processor": None,
      "gpu": None, "ram_gb": None, "storage_gb": None, "screen_inches": None,
      "edition": None},
     "not identified"),
    ("never the bare category",
     {"category": "gpu", "brand": None, "model": "RTX 3060", "gpu": None,
      "processor": None, "ram_gb": None, "storage_gb": None,
      "screen_inches": None, "edition": None},
     "RTX 3060"),
]
failed = []
for name, deal, expected in cases:
    got = describe_specs(deal)
    ok = got == expected and got not in ("gpu", "laptop", "unknown")
    print(("PASS" if ok else "FAIL"), name, "->", got)
    if not ok:
        failed.append(name)
print(f"\n{len(cases) - len(failed)}/{len(cases)} describe_specs checks passed")
if failed:
    sys.exit(1)
print("ALL DESCRIBE-SPECS CHECKS PASSED")
