import json
import shutil
import time
from pathlib import Path
import geopandas as gpd
import numpy as np

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import score as score_predictions


def get_truth_plots_and_neighbors(village):
    if village.example_truths is None:
        return []
    truth_plots = list(village.example_truths.index.astype(str))
    
    # Load neighbor graph to get 1st degree neighbors
    from bhume.loader import build_neighbor_graph
    graph = build_neighbor_graph(village.plots)
    
    plots_to_run = set(truth_plots)
    for tp in truth_plots:
        if tp in graph:
            plots_to_run.update(graph[tp].keys())
            
    return sorted(list(plots_to_run))


def evaluate_config(cfg_params, temp_debug_dir):
    # Construct Config with custom parameters
    cfg = Config(
        debug_out_dir=str(temp_debug_dir),
        workers="auto",
        cache_enabled=True,
    )
    
    # Update config parameters dynamically
    for k, v in cfg_params.items():
        setattr(cfg, k, v)
        
    coordinator = PipelineCoordinator(cfg)
    
    results = {}
    
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / name
        village = load_village(village_dir)
        plots_to_run = get_truth_plots_and_neighbors(village)
        
        # Clean predictions.geojson first to ensure fresh write
        pred_path = village_dir / "predictions.geojson"
        if pred_path.exists():
            pred_path.unlink()
            
        try:
            # Run pipeline
            res = coordinator.run_village(village_dir, plot_numbers=plots_to_run)
            
            # Score results against ground truths
            scorecard = score_predictions(pred_path, village)
            
            # Extract metrics
            results[name] = {
                "corrected": scorecard.n_corrected,
                "median_iou": scorecard.median_iou_pred if scorecard.median_iou_pred is not None else scorecard.median_iou_official,
                "improvement": scorecard.median_improvement if scorecard.median_improvement is not None else 0.0,
                "centroid_err": scorecard.median_centroid_err_m if scorecard.median_centroid_err_m is not None else 0.0,
                "spearman": scorecard.spearman_conf_vs_iou if scorecard.spearman_conf_vs_iou is not None else 0.0,
                "auc": scorecard.auc_accurate_vs_conf if scorecard.auc_accurate_vs_conf is not None else 0.0,
                "violations": len(scorecard.violations)
            }
        except Exception as e:
            print(f"Error evaluating {name}: {e}")
            results[name] = {
                "corrected": 0,
                "median_iou": 0.0,
                "improvement": 0.0,
                "centroid_err": 0.0,
                "spearman": 0.0,
                "auc": 0.0,
                "violations": 999
            }
            
        # Clean predictions.geojson after test to keep workspace clean
        if pred_path.exists():
            pred_path.unlink()
            
    return results


def run_experiments():
    print("=== Hyperparameter Grid Search Optimization ===")
    
    # We will use a temp debug output folder to prevent clutter
    temp_debug_dir = Path("debug_output_tuning")
    if temp_debug_dir.exists():
        shutil.rmtree(temp_debug_dir)
    temp_debug_dir.mkdir(parents=True, exist_ok=True)
    
    # Define Experiments to evaluate
    experiments = [
        {
            "name": "Default Baseline",
            "params": {}
        },
        {
            "name": "High DT Weight",
            "params": {
                "scoring_weights": {
                    "distance_transform": 0.6,
                    "boundary_hint": 0.1,
                    "contour_similarity": 0.1,
                    "gradient_agreement": 0.1,
                    "area_consistency": 0.05,
                    "translation_smoothness": 0.05
                }
            }
        },
        {
            "name": "Balanced Score Weights",
            "params": {
                "scoring_weights": {
                    "distance_transform": 0.35,
                    "boundary_hint": 0.35,
                    "contour_similarity": 0.1,
                    "gradient_agreement": 0.1,
                    "area_consistency": 0.05,
                    "translation_smoothness": 0.05
                }
            }
        },
        {
            "name": "High Area Penalty (lambda=3.0)",
            "params": {
                "lambda_area": 3.0
            }
        },
        {
            "name": "High Confidence Veto (0.45)",
            "params": {
                "threshold_confidence_veto": 0.45
            }
        },
        {
            "name": "Lower Correctability Veto (0.45)",
            "params": {
                "threshold_correctability": 0.45
            }
        },
        {
            "name": "Higher Regularization (factor=0.75)",
            "params": {
                "regularization_factor": 0.75
            }
        },
        {
            "name": "Tuned Combined Best Candidate",
            "params": {
                "scoring_weights": {
                    "distance_transform": 0.45,
                    "boundary_hint": 0.25,
                    "contour_similarity": 0.1,
                    "gradient_agreement": 0.1,
                    "area_consistency": 0.05,
                    "translation_smoothness": 0.05
                },
                "threshold_correctability": 0.55,
                "threshold_confidence_veto": 0.35,
                "regularization_factor": 0.6
            }
        }
    ]
    
    headers = [
        "Experiment",
        "Parameters",
        "Mala IoU",
        "Mala Imp",
        "Vadner IoU",
        "Vadner Imp",
        "Avg IoU",
        "Avg Imp"
    ]
    
    rows = []
    
    # Seed cache by running once
    print("\nSeeding cache for all plots...")
    evaluate_config({}, temp_debug_dir)
    print("Cache seeded.\n")
    
    for exp in experiments:
        name = exp["name"]
        params = exp["params"]
        
        t0 = time.perf_counter()
        res = evaluate_config(params, temp_debug_dir)
        elapsed = time.perf_counter() - t0
        
        m_iou = res["village_Malatavadi"]["median_iou"]
        m_imp = res["village_Malatavadi"]["improvement"]
        v_iou = res["village_Vadnerbhairav"]["median_iou"]
        v_imp = res["village_Vadnerbhairav"]["improvement"]
        
        avg_iou = (m_iou + v_iou) / 2.0
        avg_imp = (m_imp + v_imp) / 2.0
        
        # Display params string
        params_str = ", ".join([f"{k}={v}" for k, v in params.items()]) if params else "default"
        if len(params_str) > 40:
            params_str = params_str[:37] + "..."
            
        rows.append((
            name,
            params_str,
            f"{m_iou:.4f}",
            f"{m_imp:.4f}",
            f"{v_iou:.4f}",
            f"{v_imp:.4f}",
            f"{avg_iou:.4f}",
            f"{avg_imp:.4f}"
        ))
        print(f"Finished: {name} in {elapsed:.2f}s | Avg IoU: {avg_iou:.4f} | Avg Imp: {avg_imp:.4f}")
        
    # Print Markdown Table
    print("\n\n### Hyperparameter Tuning Results Table\n")
    print(" | ".join(headers))
    print(" | ".join(["---"] * len(headers)))
    for r in rows:
        print(" | ".join(r))
        
    # Cleanup temp debug folder
    shutil.rmtree(temp_debug_dir, ignore_errors=True)


if __name__ == "__main__":
    run_experiments()
