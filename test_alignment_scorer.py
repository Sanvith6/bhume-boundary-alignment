import sys
import time
import shutil
from pathlib import Path
import numpy as np
from bhume.config import Config
from bhume.loader import load_village, CoordinateTransformer
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator, CandidateTransformation
from bhume.alignment_scorer import AlignmentScorer, ScoredCandidate, save_score_heatmap_debug
from bhume.geo import open_imagery

def run_alignment_tests() -> bool:
    print("--- Running AlignmentScorer Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_alignment")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        scale_pyramids=[1.0],
        contour_sample_interval_m=1.0,
        debug_visualize=True,
        debug_out_dir=str(debug_dir)
    )
    # Configure custom test parameters
    cfg.sigma_dt = 2.0
    cfg.lambda_smooth = 0.02
    cfg.lambda_area = 1.0
    cfg.scoring_weights = {
        "distance_transform": 0.4,
        "boundary_hint": 0.2,
        "contour_similarity": 0.1,
        "gradient_agreement": 0.1,
        "area_consistency": 0.1,
        "translation_smoothness": 0.1,
        "shape_preservation": 0.0,
        "neighbor_consistency": 0.0,
    }
    
    preprocessor = Preprocessor(cfg)
    ms_processor = MultiScaleProcessor(cfg)
    edge_detector = EdgeDetector(cfg)
    contour_detector = ContourDetector(cfg)
    generator = CandidateGenerator(cfg)
    scorer = AlignmentScorer(cfg)
    
    # Load village
    village = load_village("data/village_Malatavadi")
    plot_no = "1"
    
    # Crop and process geometry
    with open_imagery(village.imagery_path) as src:
        transformer = CoordinateTransformer(src)
        geom_crs = transformer.geom_to_crs(village.plot(plot_no))
        patch = preprocessor.process_plot(village, plot_no)
        H, W = patch.gray.shape
        
    ms_patch = ms_processor.generate_pyramid(patch, village.plot(plot_no))
    edges = edge_detector.detect_pyramid(ms_patch, method="combined")
    edge_level = edges.get_level(1.0)
    boundary = contour_detector.parameterize_boundary(plot_no, village.plot(plot_no), transformer, image_shape=(H, W))
    
    # Generate some translation candidates
    # We will generate a grid centered at dx=5.0, dy=-5.0
    candidates = generator.generate_coarse(center_dx=5.0, center_dy=-5.0)
    
    # Evaluate a test candidate
    test_cand = candidates[0]
    rec_area = 0.0 if village.plots.loc[plot_no].get('recorded_area_sqm') is None else float(village.plots.loc[plot_no].get('recorded_area_sqm'))
    map_area = float(geom_crs.area)
    
    print(f"\nScoring single test candidate (dx={test_cand.dx:.2f}, dy={test_cand.dy:.2f})...")
    scored = scorer.score_candidate(
        test_cand,
        boundary,
        edge_level,
        transformer,
        patch.transform,
        recorded_area_m2=rec_area,
        map_area_m2=map_area
    )
    
    # 1. Verification of ScoredCandidate type
    if not isinstance(scored, ScoredCandidate):
        print(f"FAIL: Output is not a ScoredCandidate instance. Got {type(scored)}")
        return False
    print("  PASS: Output is correct ScoredCandidate type.")
        
    # 2. Score Reproducibility Check
    scored_2 = scorer.score_candidate(
        test_cand,
        boundary,
        edge_level,
        transformer,
        patch.transform,
        recorded_area_m2=rec_area,
        map_area_m2=map_area
    )
    if abs(scored.total_score - scored_2.total_score) > 1e-7:
        print(f"FAIL: Scores are not reproducible! score1={scored.total_score}, score2={scored_2.total_score}")
        return False
    print("  PASS: Score evaluation is deterministic and reproducible.")
        
    # 3. Feature Normalization Check
    for feat_name, score_val in scored.feature_scores.items():
        if score_val < 0.0 or score_val > 1.0:
            print(f"FAIL: Feature '{feat_name}' score {score_val} is outside [0, 1] range!")
            return False
    print("  PASS: All feature scores are properly normalized inside [0, 1].")
        
    # 4. Weight Effects Check
    # Verify that the total score is mathematically equal to the gated hybrid score
    w = cfg.scoring_weights
    w_dt = w.get("distance_transform", 0.45)
    w_hint = w.get("boundary_hint", 0.05)
    w_cov = w.get("contour_similarity", 0.20) * 0.5
    w_cont = w.get("contour_similarity", 0.20) * 0.5
    w_grad = w.get("gradient_agreement", 0.10)
    
    w_sum_image = w_dt + w_hint + w_cov + w_cont + w_grad
    
    f_scores = scored.feature_scores
    S_image = (
        w_dt * f_scores["distance_transform"]
        + w_hint * f_scores["boundary_hint"]
        + w_cov * f_scores["contour_similarity"]
        + w_cont * f_scores["contour_continuity"]
        + w_grad * f_scores["gradient_agreement"]
    ) / w_sum_image
    
    gate_smooth = 0.5 + 0.5 * f_scores["translation_smoothness"]
    gate_shape = 0.7 + 0.3 * f_scores["shape_preservation"]
    gate_area = 0.7 + 0.3 * f_scores["area_consistency"]
    gate_neighbor = 0.6 + 0.4 * f_scores["neighbor_consistency"]
    
    expected_total = S_image * gate_smooth * gate_shape * gate_area * gate_neighbor
    
    if abs(scored.total_score - expected_total) > 1e-6:
        print(f"FAIL: Gated weight aggregation mismatch! got {scored.total_score}, expected {expected_total}")
        return False

    print("  PASS: Weighted feature aggregation is correct.")
        
    # 5. Monotonicity and Search Grid Scoring Check
    # Let's score all coarse candidates and measure execution time (disable debug I/O during performance test)
    scorer.config.debug_visualize = False
    t_start = time.perf_counter()
    scored_list = []
    for cand in candidates:
        sc = scorer.score_candidate(
            cand,
            boundary,
            edge_level,
            transformer,
            patch.transform,
            recorded_area_m2=rec_area,
            map_area_m2=map_area
        )
        scored_list.append(sc)
    t_eval = time.perf_counter() - t_start
    scorer.config.debug_visualize = True
    avg_t_ms = (t_eval / len(candidates)) * 1000.0
    print(f"\nEvaluated {len(candidates)} candidates in {t_eval:.2f} seconds.")
    print(f"  Average candidate evaluation time: {avg_t_ms:.4f} ms")
    if avg_t_ms > 1.0: # should be well under 1ms
        print(f"WARNING: Candidate evaluation is slow: {avg_t_ms:.4f} ms")
        
    # Find best candidate
    best_scored = max(scored_list, key=lambda s: s.total_score)
    print(f"  Best Coarse candidate: dx={best_scored.candidate.dx:.2f}, dy={best_scored.candidate.dy:.2f} | score={best_scored.total_score:.4f}")
    
    # Monotonicity test: as we move away from the best candidate, the score should generally decrease.
    # Let's test a candidate far from the best candidate
    far_cand = CandidateTransformation(dx=best_scored.candidate.dx + 15.0, dy=best_scored.candidate.dy + 15.0, search_level="coarse")
    scored_far = scorer.score_candidate(
        far_cand,
        boundary,
        edge_level,
        transformer,
        patch.transform,
        recorded_area_m2=rec_area,
        map_area_m2=map_area
    )
    print(f"  Far candidate (dx={far_cand.dx:.2f}, dy={far_cand.dy:.2f}) | score={scored_far.total_score:.4f}")
    if scored_far.total_score >= best_scored.total_score:
        print(f"FAIL: Monotonicity check failed! Far candidate score {scored_far.total_score:.4f} >= best candidate score {best_scored.total_score:.4f}")
        return False
    print("  PASS: Monotonicity check passed (scores drop away from the best match).")
    
    # 6. Save Score Heatmap Debug
    heatmap_path = save_score_heatmap_debug(plot_no, scored_list, cfg.debug_out_dir)
    print(f"  Heatmap debug visualization saved at: {heatmap_path}")
    if heatmap_path is None or not heatmap_path.exists():
        print("FAIL: Expected score heatmap debug PNG was not created!")
        return False
    print("  PASS: Score heatmap visualization verified.")
    
    # Verify individual candidate debug visualization saved
    vis_file = debug_dir / "score_maps" / f"plot_{plot_no}_cand_{test_cand.candidate_id}.png"
    if not vis_file.exists():
        print(f"FAIL: Expected debug alignment file not found: {vis_file}")
        return False
    print(f"  PASS: Debug alignment file verified at {vis_file}")

    print("\n--- ALL ALIGNMENTSCORER MODULE TESTS PASSED ---")
    return True

def main():
    success = run_alignment_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
