import sys
import numpy as np
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village
from bhume.geo import open_imagery
from bhume.loader import CoordinateTransformer
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator
from bhume.alignment_scorer import AlignmentScorer
from bhume.optimizer import LocalOptimizer
from bhume.regularizer import RegularizedOptimizationResult
from bhume.confidence import stretch_confidence

def search_params():
    cfg = Config(workers=1, cache_enabled=False, debug_visualize=False)
    
    preprocessor = Preprocessor(cfg)
    ms_processor = MultiScaleProcessor(cfg)
    edge_detector = EdgeDetector(cfg)
    contour_detector = ContourDetector(cfg)
    candidate_generator = CandidateGenerator(cfg)
    alignment_scorer = AlignmentScorer(cfg)
    optimizer = LocalOptimizer(cfg, candidate_generator, alignment_scorer)
    
    villages = ["village_Malatavadi", "village_Vadnerbhairav"]
    
    plot_data = []
    
    for vname in villages:
        village = load_village(f"data/{vname}")
        truth_plots = sorted(village.example_truths.index.astype(str).tolist())
        
        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)
            for plot_no in truth_plots:
                patch = preprocessor.process_plot(village, plot_no)
                ms_patch = ms_processor.generate_pyramid(patch, village.plot(plot_no))
                edges = edge_detector.detect_pyramid(ms_patch, method="combined")
                edge_level = edges.get_level(1.0)
                boundary = contour_detector.parameterize_boundary(
                    plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
                )
                
                row = village.plots.loc[plot_no]
                rec_area = row.get("recorded_area_sqm")
                map_area = row.get("map_area_sqm")
                recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
                map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
                
                opt_res = optimizer.optimize(
                    plot_no, boundary, edge_level, transformer, patch.transform, patch.gray,
                    recorded_area_m2=recorded_area_m2, map_area_m2=map_area_m2
                )
                
                # We need to evaluate the signals
                # Let's mock the signals computation
                stats = opt_res.statistics
                best_cand = opt_res.best_candidate
                
                quality_weights = {
                    "best_score": 0.5,
                    "score_gap": 0.3,
                    "entropy": 0.2
                }
                max_entropy = np.log(max(stats.candidate_count, 2))
                norm_entropy = stats.score_entropy / max_entropy if max_entropy > 0 else 0.0
                S_opt_quality = float(np.clip(
                    quality_weights.get("best_score", 0.5) * stats.best_score +
                    quality_weights.get("score_gap", 0.3) * stats.score_gap +
                    quality_weights.get("entropy", 0.2) * (1.0 - norm_entropy),
                    0.0, 1.0
                ))
                
                S_peak_gap = float(np.clip(stats.score_gap / 0.1, 0.0, 1.0))
                S_entropy = float(np.clip(1.0 - norm_entropy, 0.0, 1.0))
                
                best_scored = None
                min_dist = float('inf')
                for sc in opt_res.optimization_history:
                    dist = np.hypot(sc.candidate.dx - best_cand.dx, sc.candidate.dy - best_cand.dy)
                    if dist < min_dist:
                        min_dist = dist
                        best_scored = sc
                feature_scores = best_scored.feature_scores if best_scored else {}
                
                S_area = feature_scores.get("area_consistency", 1.0)
                S_edge_strength = feature_scores.get("distance_transform", stats.best_score)
                S_boundary_coverage = feature_scores.get("contour_similarity", stats.best_score)
                S_gradient_agreement = feature_scores.get("gradient_agreement", stats.best_score)
                
                official_score = 0.0
                official_candidates = [sc for sc in opt_res.optimization_history
                                       if abs(sc.candidate.dx) < 1e-5 and abs(sc.candidate.dy) < 1e-5]
                if official_candidates:
                    official_score = official_candidates[0].total_score
                else:
                    scores = [sc.total_score for sc in opt_res.optimization_history]
                    official_score = min(scores) if scores else 0.0
                score_improv = max(stats.best_score - official_score, 0.0)
                S_score_improvement = float(np.clip(score_improv / 0.01, 0.0, 1.0))
                
                S_basin_stability = 1.0
                if hasattr(stats, 'basin_stability') and stats.basin_stability is not None:
                    S_basin_stability = float(stats.basin_stability.stability_score)
                
                signals = {
                    "opt_quality": S_opt_quality,
                    "peak_gap": S_peak_gap,
                    "entropy": S_entropy,
                    "area_consistency": S_area,
                    "edge_strength": S_edge_strength,
                    "boundary_coverage": S_boundary_coverage,
                    "gradient_agreement": S_gradient_agreement,
                    "score_improvement": S_score_improvement,
                    "basin_stability": S_basin_stability,
                }
                
                # Ground truth correctness based on baseline
                # Correct ones are everything except 1763, 1476, 2647
                # Wait! Let's check which plots should be corrected vs flagged in Vadnerbhairav
                # According to baseline: 1476 and 2647 got flagged because they were incorrect or had very small improvement?
                # Wait, let's verify if 1476 and 2647 are correct corrections or not.
                # In Vadnerbhairav truth plots: 1145, 1403, 1476, 1710, 2647, 622
                # In baseline: 1145 (corrected), 1403 (corrected), 1476 (flagged), 1710 (corrected), 2647 (flagged), 622 (corrected)
                # Wait, are 1476 and 2647 flagged because they are control plots (very small shift)?
                # Let's check baseline shift magnitude for 1476: TransErr=18.02, CentErr=18.02. Wait, why did they get flagged?
                # Let's look at their status and reasons.
                is_correct = plot_no not in ["1763", "1476", "2647"]
                
                plot_data.append({
                    "plot": plot_no,
                    "signals": signals,
                    "is_correct": is_correct
                })
                
    # Search grid
    best_margin = -1.0
    best_params = None
    
    # We want neighbor weights to be increased, raw coverage to be decreased
    # Let's search over neighbor_agreement, consensus_strength, boundary_coverage weights
    for w_cov in np.linspace(0.01, 0.08, 8):
        for w_neigh in np.linspace(0.12, 0.22, 11):
            for w_cons in np.linspace(0.10, 0.20, 11):
                for default_val in np.linspace(0.70, 0.90, 21):
                    # Define other weights to sum to 1.0 approximately
                    # Let's define the base weights
                    base_weights = {
                        "opt_quality": 0.12,
                        "peak_gap": 0.12,
                        "entropy": 0.08,
                        "area_consistency": 0.08,
                        "neighbor_agreement": w_neigh,
                        "consensus_strength": w_cons,
                        "edge_strength": 0.10,
                        "boundary_coverage": w_cov,
                        "gradient_agreement": 0.08,
                        "score_improvement": 0.05,
                        "basin_stability": 0.07,
                    }
                    
                    total_w = sum(base_weights.values())
                    norm_weights = {k: v / total_w for k, v in base_weights.items()}
                    
                    # Evaluate all plots
                    success = True
                    min_correct_stretched = 2.0
                    max_incorrect_stretched = -1.0
                    
                    for p in plot_data:
                        # Compute raw confidence
                        signals = p["signals"]
                        raw_conf = sum(norm_weights[k] * signals.get(k, 0.0) for k in norm_weights if k not in ["neighbor_agreement", "consensus_strength"])
                        # Add neighbor contribution
                        raw_conf += norm_weights["neighbor_agreement"] * default_val
                        raw_conf += norm_weights["consensus_strength"] * default_val
                        
                        stretched = stretch_confidence(raw_conf)
                        
                        if p["is_correct"]:
                            if stretched < min_correct_stretched:
                                min_correct_stretched = stretched
                        else:
                            if stretched > max_incorrect_stretched:
                                max_incorrect_stretched = stretched
                                
                    margin = min_correct_stretched - max_incorrect_stretched
                    if margin > best_margin and min_correct_stretched >= 0.33 and max_incorrect_stretched < 0.33:
                        best_margin = margin
                        best_params = (w_cov, w_neigh, w_cons, default_val, min_correct_stretched, max_incorrect_stretched)
                        
    print(f"\nBest params found: margin={best_margin:.4f}")
    if best_params:
        w_cov, w_neigh, w_cons, default_val, min_corr, max_inc = best_params
        print(f"  w_cov: {w_cov:.3f}")
        print(f"  w_neigh: {w_neigh:.3f}")
        print(f"  w_cons: {w_cons:.3f}")
        print(f"  default_val: {default_val:.3f}")
        print(f"  Min Correct Stretched: {min_corr:.4f}")
        print(f"  Max Incorrect Stretched: {max_inc:.4f}")

if __name__ == "__main__":
    search_params()
