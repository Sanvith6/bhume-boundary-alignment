import sys
import shutil
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scratch.benchmark_runner import run_benchmark
from bhume.config import Config

def run():
    cfg = Config(workers="auto", cache_enabled=False)
    
    # 1. Run Malatavadi
    print("Running Malatavadi...")
    run_benchmark(config=cfg, villages=["village_Malatavadi"], verbose=False)
    # Copy stats
    src_stats = Path("debug_output/predictions/decision_statistics.csv")
    dest_stats_m = Path("debug_output/predictions/decision_statistics_Malatavadi.csv")
    if src_stats.exists():
        shutil.copy(src_stats, dest_stats_m)
        print(f"Saved Malatavadi stats to {dest_stats_m}")
        
    # 2. Run Vadnerbhairav
    print("Running Vadnerbhairav...")
    run_benchmark(config=cfg, villages=["village_Vadnerbhairav"], verbose=False)
    dest_stats_v = Path("debug_output/predictions/decision_statistics_Vadnerbhairav.csv")
    if src_stats.exists():
        shutil.copy(src_stats, dest_stats_v)
        print(f"Saved Vadnerbhairav stats to {dest_stats_v}")

if __name__ == "__main__":
    run()
