"""Quick smoke test of the improved pipeline on public truth plots only."""

import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import score as score_predictions


def run_quick_test():
    """Run pipeline on only public truth plots for fast validation."""
    cfg = Config(
        workers=1,
        cache_enabled=False,
        debug_visualize=False,
    )

    coordinator = PipelineCoordinator(cfg)

    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / name
        village = load_village(village_dir)
        
        if village.example_truths is None:
            print(f"No truths for {name}")
            continue
        
        truth_plots = sorted(village.example_truths.index.astype(str).tolist())
        print(f"\n{name}: testing {len(truth_plots)} truth plots: {truth_plots}")
        
        t0 = time.perf_counter()
        try:
            res = coordinator.run_village(village_dir, plot_numbers=truth_plots)
            scorecard = score_predictions(village_dir / "predictions.geojson", village)
            elapsed = time.perf_counter() - t0
            print(scorecard)
            print(f"Runtime: {elapsed:.2f}s")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"ERROR: {e}")


if __name__ == "__main__":
    run_quick_test()
