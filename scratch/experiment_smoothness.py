"""Experiment 1: Test impact of reducing translation_smoothness penalty."""
import sys
import json
sys.path.insert(0, '.')

from bhume.config import Config
from scratch.benchmark_runner import run_benchmark, load_baseline, compare_metrics, save_baseline

# Load current baseline
old_metrics, old_results = load_baseline()

# Experiment configs to try
experiments = [
    {
        "name": "smooth_w0.10_lam0.02",
        "scoring_weights": {
            "distance_transform": 0.50,
            "boundary_hint": 0.05,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.10,  # Was 0.20
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        },
        "lambda_smooth": 0.02,  # Was 0.04
    },
    {
        "name": "smooth_w0.05_lam0.02",
        "scoring_weights": {
            "distance_transform": 0.55,
            "boundary_hint": 0.05,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.05,  # Minimal
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        },
        "lambda_smooth": 0.02,
    },
    {
        "name": "smooth_w0.05_lam0.01",
        "scoring_weights": {
            "distance_transform": 0.55,
            "boundary_hint": 0.05,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.05,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        },
        "lambda_smooth": 0.01,
    },
    {
        "name": "no_smooth",
        "scoring_weights": {
            "distance_transform": 0.55,
            "boundary_hint": 0.05,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.0,  # Zero
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        },
        "lambda_smooth": 0.0,
    },
    {
        "name": "smooth_w0.10_lam0.01",
        "scoring_weights": {
            "distance_transform": 0.50,
            "boundary_hint": 0.05,
            "contour_similarity": 0.15,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.10,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        },
        "lambda_smooth": 0.01,
    },
]

best_name = "baseline"
best_metrics = old_metrics
best_config_data = None

for exp in experiments:
    print(f"\n{'='*60}")
    print(f"EXPERIMENT: {exp['name']}")
    print(f"{'='*60}")
    
    cfg = Config(
        workers=1,
        cache_enabled=False,
        scoring_weights=exp["scoring_weights"],
        lambda_smooth=exp["lambda_smooth"],
    )
    
    metrics, results = run_benchmark(config=cfg, verbose=False)
    
    print(f"  Mean IoU:    {metrics['mean_iou']:.4f}")
    print(f"  Min IoU:     {metrics['min_iou']:.4f}")
    print(f"  Accurate:    {metrics['accurate_rate']:.4f}")
    print(f"  Spearman:    {metrics['spearman_conf_iou']:.4f}")
    print(f"  Corrected:   {metrics['corrected_count']}")
    
    if best_metrics is None or metrics['mean_iou'] > best_metrics['mean_iou']:
        print(f"  >>> NEW BEST! mean_iou improved from {best_metrics['mean_iou']:.4f} to {metrics['mean_iou']:.4f}")
        best_name = exp['name']
        best_metrics = metrics
        best_config_data = exp
        best_results = results

print(f"\n{'='*60}")
print(f"BEST EXPERIMENT: {best_name}")
print(f"Best Mean IoU: {best_metrics['mean_iou']:.4f}")
print(f"Best Config: {json.dumps(best_config_data, indent=2) if best_config_data else 'baseline'}")
print(f"{'='*60}")

if best_config_data:
    # Save new baseline
    save_baseline(best_metrics, best_results, "scratch/baseline.json")
    # Save best config
    with open("scratch/best_experiment.json", "w") as f:
        json.dump(best_config_data, f, indent=2)
