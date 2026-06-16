"""Experiment 2: Broader search with different sigma_dt and weight combinations.

Key insight: Plot 1763 has true shift at 14m. The EDT signal at the true position
scores 0.590 vs 0.705 at the wrong position. We need to either:
1. Increase sigma_dt so the EDT is less sensitive (broader attraction basin)
2. Change search radius
3. Increase boundary_hint or contour_similarity weight to give more discriminative power 
4. Use a combination approach
"""
import sys
import json
sys.path.insert(0, '.')

from bhume.config import Config
from scratch.benchmark_runner import run_benchmark, load_baseline, save_baseline

old_metrics, old_results = load_baseline()

experiments = [
    # Test sigma_dt changes - higher sigma means broader attraction basin
    {
        "name": "sigma_dt_3.0",
        "sigma_dt": 3.0,
    },
    {
        "name": "sigma_dt_4.0",
        "sigma_dt": 4.0,
    },
    {
        "name": "sigma_dt_5.0",
        "sigma_dt": 5.0,
    },
    # Test DT weight reduction + boundary_hint increase
    {
        "name": "dt0.35_hint0.15_cov0.20",
        "scoring_weights": {
            "distance_transform": 0.35,
            "boundary_hint": 0.15,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.15,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        },
    },
    # More aggressive: rely more on coverage and gradient
    {
        "name": "dt0.35_hint0.10_cov0.20_grad0.15",
        "scoring_weights": {
            "distance_transform": 0.35,
            "boundary_hint": 0.10,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.15,
            "area_consistency": 0.05,
            "translation_smoothness": 0.15,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        },
    },
    # Combine sigma_dt change with weight rebalance
    {
        "name": "sigma3_dt0.40_hint0.10_smooth0.15",
        "sigma_dt": 3.0,
        "scoring_weights": {
            "distance_transform": 0.40,
            "boundary_hint": 0.10,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.15,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        },
    },
    # Lower smoothness with sigma_dt=3
    {
        "name": "sigma3_smooth0.10_lam0.02",
        "sigma_dt": 3.0,
        "lambda_smooth": 0.02,
        "scoring_weights": {
            "distance_transform": 0.45,
            "boundary_hint": 0.10,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.10,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        },
    },
    # Try sigma_dt=4 with reduced smoothness
    {
        "name": "sigma4_smooth0.10_lam0.02",
        "sigma_dt": 4.0,
        "lambda_smooth": 0.02,
        "scoring_weights": {
            "distance_transform": 0.45,
            "boundary_hint": 0.10,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.10,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        },
    },
]

best_name = "baseline"
best_metrics = old_metrics
best_config_data = None
best_results_data = None

for exp in experiments:
    print(f"\n{'='*60}")
    print(f"EXPERIMENT: {exp['name']}")
    print(f"{'='*60}")
    
    kwargs = {"workers": 1, "cache_enabled": False}
    if "sigma_dt" in exp:
        kwargs["sigma_dt"] = exp["sigma_dt"]
    if "lambda_smooth" in exp:
        kwargs["lambda_smooth"] = exp["lambda_smooth"]
    if "scoring_weights" in exp:
        kwargs["scoring_weights"] = exp["scoring_weights"]
    
    cfg = Config(**kwargs)
    
    metrics, results = run_benchmark(config=cfg, verbose=False)
    
    # Print per-plot detail
    for r in results:
        status_mark = "OK" if r["iou"] >= 0.5 else "XX"
        print(f"  {status_mark} {r['village'][-10:]}/{r['plot']}: IoU={r['iou']:.4f} conf={r['confidence']:.4f} centErr={r['centroid_err']:.1f}m")
    
    print(f"  --- SUMMARY ---")
    print(f"  Mean IoU:    {metrics['mean_iou']:.4f} (baseline: {old_metrics['mean_iou']:.4f})")
    print(f"  Min IoU:     {metrics['min_iou']:.4f} (baseline: {old_metrics['min_iou']:.4f})")
    print(f"  Accurate:    {metrics['accurate_rate']:.4f}")
    print(f"  Spearman:    {metrics['spearman_conf_iou']:.4f}")
    
    if metrics['mean_iou'] > best_metrics['mean_iou']:
        print(f"  >>> NEW BEST! mean_iou: {best_metrics['mean_iou']:.4f} -> {metrics['mean_iou']:.4f}")
        best_name = exp['name']
        best_metrics = metrics
        best_config_data = exp
        best_results_data = results

print(f"\n{'='*60}")
print(f"BEST EXPERIMENT: {best_name}")
print(f"Best Mean IoU: {best_metrics['mean_iou']:.4f}")
if best_config_data:
    print(f"Best Config: {json.dumps(best_config_data, indent=2)}")
print(f"{'='*60}")

if best_config_data:
    save_baseline(best_metrics, best_results_data, "scratch/baseline.json")
    with open("scratch/best_experiment.json", "w") as f:
        json.dump(best_config_data, f, indent=2)
