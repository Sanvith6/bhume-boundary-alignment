"""Run the pipeline on the representative benchmark set and record comprehensive metrics.

Usage:
    python scratch/evaluate_benchmark.py
    python scratch/evaluate_benchmark.py --overrides '{"threshold_confidence_veto": 0.55}'
"""

import json
import sys
import time
import shutil
from pathlib import Path

import numpy as np
import geopandas as gpd

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import score as score_predictions


def run_benchmark(config_overrides=None):
    if config_overrides is None:
        config_overrides = {}

    print(f"--- Running Benchmark with overrides: {config_overrides} ---")

    # Load benchmark plots
    with open("scratch/benchmark_plots.json", "r") as f:
        benchmark_plots = json.load(f)

    # Set up config — always sequential, always cache
    cfg = Config(
        workers=1,
        cache_enabled=True,
        debug_visualize=False,
    )
    for k, v in config_overrides.items():
        setattr(cfg, k, v)

    coordinator = PipelineCoordinator(cfg)

    results = {}

    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / name
        village = load_village(village_dir)
        plots_to_run = benchmark_plots.get(name, [])

        if not plots_to_run:
            print(f"No benchmark plots for {name}, skipping.")
            continue

        # Temp prediction path to avoid overwriting predictions.geojson in data directory
        temp_pred_path = Path("scratch") / f"{name}_temp_preds.geojson"
        if temp_pred_path.exists():
            temp_pred_path.unlink()

        t0 = time.perf_counter()

        original_pred_path = village_dir / "predictions.geojson"
        backup_pred_path = village_dir / "predictions.geojson.backup"
        if original_pred_path.exists():
            shutil.copy(original_pred_path, backup_pred_path)

        try:
            res = coordinator.run_village(village_dir, plot_numbers=plots_to_run)

            # Score results against ground truths
            scorecard = score_predictions(original_pred_path, village)

            # Save predictions.geojson to temp path and restore original/backup
            shutil.move(str(original_pred_path), str(temp_pred_path))
            if backup_pred_path.exists():
                shutil.move(str(backup_pred_path), str(original_pred_path))

            elapsed = time.perf_counter() - t0

            results[name] = {
                "corrected": scorecard.n_corrected,
                "flagged": scorecard.n_flagged,
                "median_iou": scorecard.median_iou_pred if scorecard.median_iou_pred is not None else scorecard.median_iou_official,
                "improvement": scorecard.median_improvement if scorecard.median_improvement is not None else 0.0,
                "centroid_err": scorecard.median_centroid_err_m if scorecard.median_centroid_err_m is not None else 0.0,
                "accurate_rate": scorecard.accurate_rate if scorecard.accurate_rate is not None else 0.0,
                "spearman": scorecard.spearman_conf_vs_iou if scorecard.spearman_conf_vs_iou is not None else 0.0,
                "auc": scorecard.auc_accurate_vs_conf if scorecard.auc_accurate_vs_conf is not None else 0.0,
                "runtime": elapsed,
                "total_benchmark_plots": len(plots_to_run),
                "scorecard": str(scorecard),
            }
        except Exception as e:
            print(f"Error running pipeline on {name}: {e}")
            import traceback
            traceback.print_exc()
            if backup_pred_path.exists():
                shutil.move(str(backup_pred_path), str(original_pred_path))
            results[name] = {
                "corrected": 0,
                "flagged": 0,
                "median_iou": 0.0,
                "improvement": 0.0,
                "centroid_err": 0.0,
                "accurate_rate": 0.0,
                "spearman": 0.0,
                "auc": 0.0,
                "runtime": 0.0,
                "total_benchmark_plots": len(plots_to_run),
                "scorecard": f"Error: {e}",
            }

    # Print combined results
    print("\n================ BENCHMARK RESULTS ================")
    for name, res in results.items():
        print(res["scorecard"])
        print(f"Benchmark plots: {res['total_benchmark_plots']}")
        print(f"Runtime: {res['runtime']:.2f} seconds\n")

    # Save results
    out_path = Path("scratch") / "baseline.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=4)
    print(f"Saved baseline metrics to {out_path}")

    return results


if __name__ == "__main__":
    overrides = {}
    if len(sys.argv) > 2 and sys.argv[1] == "--overrides":
        overrides = json.loads(sys.argv[2])
    run_benchmark(overrides)
