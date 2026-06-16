import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from scratch.benchmark_runner import run_benchmark

def main():
    print("Starting confidence weight search...")
    
    # We will search over values that satisfy the user requirements:
    # - INCREASE neighbor_agreement (currently 0.12) -> e.g. [0.15, 0.20, 0.25]
    # - INCREASE peak_gap (currently 0.12) -> e.g. [0.15, 0.20, 0.25]
    # - DECREASE edge_strength (currently 0.10) -> e.g. [0.01, 0.05]
    # - DECREASE boundary_coverage (currently 0.01) -> e.g. [0.0, 0.005]
    # - Also prioritize search stability (basin_stability, currently 0.07) and consensus (consensus_strength, currently 0.10)
    
    # Let's define candidates for the search:
    neighbor_agreement_opts = [0.15, 0.20, 0.25]
    peak_gap_opts = [0.15, 0.20, 0.25]
    edge_strength_opts = [0.01, 0.03, 0.05]
    boundary_coverage_opts = [0.0, 0.005]
    
    # Let's keep others relatively stable, maybe search over basin_stability and consensus_strength
    basin_stability_opts = [0.10, 0.15]
    consensus_strength_opts = [0.12, 0.15]
    
    best_spearman = -2.0
    best_weights = None
    best_metrics = None
    
    # Run with cache enabled to make it super fast!
    base_config = Config(workers=1, cache_enabled=True)
    
    # Run once to warm up the cache
    run_benchmark(base_config, verbose=False)
    
    count = 0
    for na in neighbor_agreement_opts:
        for pg in peak_gap_opts:
            for es in edge_strength_opts:
                for bc in boundary_coverage_opts:
                    for bs in basin_stability_opts:
                        for cs in consensus_strength_opts:
                            weights = {
                                "opt_quality": 0.10,
                                "peak_gap": pg,
                                "entropy": 0.05,
                                "area_consistency": 0.05,
                                "neighbor_agreement": na,
                                "consensus_strength": cs,
                                "edge_strength": es,
                                "boundary_coverage": bc,
                                "gradient_agreement": 0.05,
                                "score_improvement": 0.05,
                                "basin_stability": bs,
                            }
                            # Normalize weights to sum to 1.0
                            total = sum(weights.values())
                            norm_weights = {k: v / total for k, v in weights.items()}
                            
                            cfg = Config(
                                workers=1,
                                cache_enabled=True,
                                confidence_signal_weights=norm_weights
                            )
                            
                            count += 1
                            try:
                                metrics, _ = run_benchmark(cfg, verbose=False)
                                spearman = metrics["spearman_conf_iou"]
                                
                                if spearman > best_spearman:
                                    best_spearman = spearman
                                    best_weights = norm_weights
                                    best_metrics = metrics
                                    print(f"Iteration {count}: New best Spearman = {best_spearman:.4f}")
                                    print(f"Weights: {best_weights}\n")
                            except Exception as e:
                                print(f"Error in iteration {count}: {e}")
                                
    print("Search complete!")
    print(f"Best Spearman Correlation: {best_spearman:.4f}")
    print("Best Weights:")
    for k, v in best_weights.items():
        print(f"  '{k}': {v:.4f},")
    print("Best Metrics:")
    for k, v in best_metrics.items():
        print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
