"""Benchmark runner for evaluating pipeline on example truth plots.

Runs the full pipeline on only the example truth plots, computes metrics,
and compares against baseline.

WARNING: this writes predictions.geojson into the village directory containing
ONLY the benchmark plots. Always re-run the FULL village pipeline afterwards
before uploading or submitting predictions.
"""
import sys
import json
import time
import numpy as np
import geopandas as gpd
from pathlib import Path
from dataclasses import dataclass, asdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.io import load as load_village
from bhume.score import _utm_for, _iou, score as score_predictions
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator


def run_benchmark(config=None, villages=None, verbose=True):
    """Run pipeline on example truth plots and compute metrics."""
    if config is None:
        config = Config(workers="auto", cache_enabled=False)

    if villages is None:
        villages = ["village_Malatavadi", "village_Vadnerbhairav"]

    all_results = []
    scorecards = []

    for vname in villages:
        village_dir = Path("data") / vname
        if not village_dir.exists():
            continue

        village = load_village(village_dir)
        truth = village.example_truths
        if truth is None:
            continue

        truth_plots = sorted(truth.index.astype(str).tolist())

        if verbose:
            print(f"\n--- {vname}: Processing {len(truth_plots)} truth plots ---")

        # Run pipeline on truth plots only
        t0 = time.perf_counter()
        coordinator = PipelineCoordinator(config)
        result = coordinator.run_village(village_dir, plot_numbers=truth_plots)
        runtime = time.perf_counter() - t0

        if verbose:
            print(f"Runtime: {runtime:.1f}s")

        # Evaluate
        pred_path = village_dir / "predictions.geojson"
        pred = gpd.read_file(pred_path).set_index("plot_number", drop=False)
        official = village.plots

        utm = _utm_for(truth.geometry.iloc[0])
        truth_u = truth.to_crs(utm)
        official_u = official.to_crs(utm)
        pred_u = pred.to_crs(utm)

        for pn in truth_plots:
            t_geom = truth_u.loc[pn, 'geometry']
            o_geom = official_u.loc[pn, 'geometry']

            p_row = pred.loc[pn]
            p_status = str(p_row.get("status", ""))
            p_conf = float(p_row.get("confidence", 0.0))

            pg = pred_u.loc[pn, 'geometry']

            iou = _iou(pg, t_geom)
            iou_official = _iou(o_geom, t_geom)
            centroid_err = pg.centroid.distance(t_geom.centroid)

            # True shift
            dx_true = t_geom.centroid.x - o_geom.centroid.x
            dy_true = t_geom.centroid.y - o_geom.centroid.y

            # Predicted shift
            dx_pred = pg.centroid.x - o_geom.centroid.x
            dy_pred = pg.centroid.y - o_geom.centroid.y

            translation_err = np.hypot(dx_pred - dx_true, dy_pred - dy_true)

            all_results.append({
                "village": vname,
                "plot": pn,
                "status": p_status,
                "confidence": p_conf,
                "iou": iou,
                "iou_official": iou_official,
                "improvement": iou - iou_official,
                "centroid_err": centroid_err,
                "translation_err": translation_err,
                "dx_true": dx_true,
                "dy_true": dy_true,
                "dx_pred": dx_pred,
                "dy_pred": dy_pred,
            })

        # Get scorecard
        try:
            sc = score_predictions(pred_path, village)
            scorecards.append(sc)
            if verbose:
                print(sc)
        except Exception as e:
            print(f"Scoring failed: {e}")

        if verbose:
            print(f"WARNING: {pred_path} now contains ONLY the {len(truth_plots)} benchmark plots.")
            print(f"  Re-run the FULL village pipeline before uploading/submitting predictions.")

    # Compute aggregate metrics
    ious = [r["iou"] for r in all_results]
    confs = [r["confidence"] for r in all_results if r["status"] == "corrected"]
    corrected_ious = [r["iou"] for r in all_results if r["status"] == "corrected"]

    from scipy.stats import spearmanr
    conf_iou_pairs = [(r["confidence"], r["iou"]) for r in all_results if r["status"] == "corrected"]

    if len(conf_iou_pairs) >= 3:
        cs, iz = zip(*conf_iou_pairs)
        if len(set(cs)) > 1 and len(set(iz)) > 1:
            spearman = spearmanr(cs, iz).correlation
        else:
            spearman = 0.0
    else:
        spearman = 0.0

    metrics = {
        "mean_iou": float(np.mean(ious)),
        "median_iou": float(np.median(ious)),
        "min_iou": float(np.min(ious)),
        "accurate_rate": float(np.mean([1 if iou >= 0.5 else 0 for iou in corrected_ious])) if corrected_ious else 0.0,
        "improvement_frac": float(np.mean([1 if r["improvement"] > 0 else 0 for r in all_results if r["status"] == "corrected"])) if any(r["status"] == "corrected" for r in all_results) else 0.0,
        "spearman_conf_iou": float(spearman),
        "mean_confidence": float(np.mean(confs)) if confs else 0.0,
        "corrected_count": sum(1 for r in all_results if r["status"] == "corrected"),
        "flagged_count": sum(1 for r in all_results if r["status"] == "flagged"),
        "mean_centroid_err": float(np.mean([r["centroid_err"] for r in all_results if r["status"] == "corrected"])) if any(r["status"] == "corrected" for r in all_results) else 0.0,
    }

    if verbose:
        print(f"\n=== AGGREGATE METRICS ===")
        for k, v in metrics.items():
            print(f"  {k}: {v:.4f}")
        print()
        print("Per-plot results:")
        print(f"{'Village':<25} {'Plot':<8} {'Status':<10} {'Conf':>6} {'IoU':>6} {'IoU_off':>7} {'CentErr':>8} {'TransErr':>9}")
        for r in all_results:
            print(f"{r['village']:<25} {r['plot']:<8} {r['status']:<10} {r['confidence']:>6.4f} {r['iou']:>6.4f} {r['iou_official']:>7.4f} {r['centroid_err']:>8.2f} {r['translation_err']:>9.2f}")

    return metrics, all_results


def save_baseline(metrics, results, path="scratch/baseline.json"):
    """Save baseline metrics to disk."""
    data = {"metrics": metrics, "results": results}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Baseline saved to {path}")


def load_baseline(path="scratch/baseline.json"):
    """Load baseline metrics from disk."""
    if not Path(path).exists():
        return None, None
    with open(path) as f:
        data = json.load(f)
    return data["metrics"], data["results"]


def compare_metrics(new_metrics, old_metrics):
    """Compare new metrics against old baseline. Returns True if improved."""
    if old_metrics is None:
        return True

    # Primary metric: mean IoU
    iou_improved = new_metrics["mean_iou"] > old_metrics["mean_iou"] - 0.001

    # Secondary: Spearman should be positive and ideally higher
    spearman_ok = new_metrics["spearman_conf_iou"] >= old_metrics["spearman_conf_iou"] - 0.05

    # Accurate rate should not decrease
    acc_ok = new_metrics["accurate_rate"] >= old_metrics["accurate_rate"] - 0.01

    improved = new_metrics["mean_iou"] > old_metrics["mean_iou"] + 0.001

    print(f"\n=== COMPARISON ===")
    print(f"  Mean IoU:    {old_metrics['mean_iou']:.4f} -> {new_metrics['mean_iou']:.4f} ({'BETTER' if new_metrics['mean_iou'] > old_metrics['mean_iou'] else 'WORSE'})")
    print(f"  Spearman:    {old_metrics['spearman_conf_iou']:.4f} -> {new_metrics['spearman_conf_iou']:.4f}")
    print(f"  Accurate:    {old_metrics['accurate_rate']:.4f} -> {new_metrics['accurate_rate']:.4f}")
    print(f"  Min IoU:     {old_metrics['min_iou']:.4f} -> {new_metrics['min_iou']:.4f}")
    print(f"  Corrected:   {old_metrics['corrected_count']} -> {new_metrics['corrected_count']}")

    return improved


if __name__ == "__main__":
    metrics, results = run_benchmark()
    save_baseline(metrics, results)
