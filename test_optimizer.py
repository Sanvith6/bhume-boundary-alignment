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
from bhume.candidate_generator import CandidateGenerator
from bhume.alignment_scorer import AlignmentScorer
from bhume.optimizer import LocalOptimizer, OptimizationResult
from bhume.geo import open_imagery

def run_optimizer_tests() -> bool:
    print("--- Running LocalOptimizer Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_optimizer")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        scale_pyramids=[1.0],
        contour_sample_interval_m=1.0,
        search_radius_m=20.0,
        search_step_m=1.0,
        fine_search_radius_m=2.0,
        fine_search_step_m=0.25,
        debug_visualize=True,
        debug_out_dir=str(debug_dir)
    )
    
    preprocessor = Preprocessor(cfg)
    ms_processor = MultiScaleProcessor(cfg)
    edge_detector = EdgeDetector(cfg)
    contour_detector = ContourDetector(cfg)
    generator = CandidateGenerator(cfg)
    scorer = AlignmentScorer(cfg)
    optimizer = LocalOptimizer(cfg, generator, scorer)
    
    # Load village
    village = load_village("data/village_Malatavadi")
    plot_no = "1"
    
    # Process inputs
    with open_imagery(village.imagery_path) as src:
        transformer = CoordinateTransformer(src)
        geom_crs = transformer.geom_to_crs(village.plot(plot_no))
        patch = preprocessor.process_plot(village, plot_no)
        H, W = patch.gray.shape
        
    ms_patch = ms_processor.generate_pyramid(patch, village.plot(plot_no))
    edges = edge_detector.detect_pyramid(ms_patch, method="combined")
    edge_level = edges.get_level(1.0)
    boundary = contour_detector.parameterize_boundary(plot_no, village.plot(plot_no), transformer, image_shape=(H, W))
    
    rec_area = 0.0 if village.plots.loc[plot_no].get('recorded_area_sqm') is None else float(village.plots.loc[plot_no].get('recorded_area_sqm'))
    map_area = float(geom_crs.area)
    
    # 1. Run Optimization & Measure Execution Time
    print(f"\nRunning local optimization for Plot {plot_no}...")
    t_start = time.perf_counter()
    res = optimizer.optimize(
        plot_no,
        boundary,
        edge_level,
        transformer,
        patch.transform,
        patch.gray,
        center_dx=0.0,
        center_dy=0.0,
        recorded_area_m2=rec_area,
        map_area_m2=map_area
    )
    t_optim = time.perf_counter() - t_start
    print(f"  Optimized in {t_optim * 1000:.2f} ms ({res.evaluated_candidate_count} candidates evaluated)")
    
    if not isinstance(res, OptimizationResult):
        print(f"FAIL: Output is not an OptimizationResult. Got {type(res)}")
        return False
    print("  PASS: Output is correct OptimizationResult type.")
    
    # 2. Verify Top-K Candidates Preservation
    print("\nVerifying Top-K Candidates...")
    if not hasattr(res, "top_k_candidates") or not res.top_k_candidates:
        print("FAIL: top_k_candidates not found or empty.")
        return False
    print(f"  Found {len(res.top_k_candidates)} Top-K candidates (config K={cfg.top_k_candidates}).")
    if len(res.top_k_candidates) > cfg.top_k_candidates:
        print(f"FAIL: Top-K candidate count exceeds configured limit. Got {len(res.top_k_candidates)}, expected <= {cfg.top_k_candidates}")
        return False
    # Check sorting
    for idx in range(len(res.top_k_candidates) - 1):
        if res.top_k_candidates[idx].total_score < res.top_k_candidates[idx+1].total_score:
            print("FAIL: Top-K candidates are not sorted in descending order of score.")
            return False
    print("  PASS: Top-K candidates correctly preserved and sorted.")

    # 3. Verify Optimization Statistics
    print("\nVerifying Optimization Statistics...")
    if not hasattr(res, "statistics") or res.statistics is None:
        print("FAIL: statistics object not found.")
        return False
    stats = res.statistics
    print(f"  Best score: {stats.best_score:.4f}")
    print(f"  Second best score: {stats.second_best_score:.4f}")
    print(f"  Score gap: {stats.score_gap:.4f}")
    print(f"  Score mean: {stats.score_mean:.4f}")
    print(f"  Score std: {stats.score_std:.4f}")
    print(f"  Score entropy: {stats.score_entropy:.4f}")
    print(f"  Number of local maxima: {stats.number_of_local_maxima}")
    print(f"  Optimum on boundary: {stats.optimum_on_boundary}")
    print(f"  Convergence path: {stats.convergence_path}")
    
    # Assertions on statistics values
    if stats.best_score != res.best_score:
        print(f"FAIL: best_score mismatch. Got {stats.best_score}, expected {res.best_score}")
        return False
    if abs(stats.score_gap - (stats.best_score - stats.second_best_score)) > 1e-7:
        print(f"FAIL: score_gap math incorrect. Gap={stats.score_gap}, difference={stats.best_score - stats.second_best_score}")
        return False
    if stats.score_std < 0.0:
        print("FAIL: standard deviation is negative.")
        return False
    if stats.score_entropy <= 0.0 and len(res.optimization_history) > 1:
        print("FAIL: entropy is zero or negative for non-trivial search.")
        return False
    if not isinstance(stats.optimum_on_boundary, bool):
        print("FAIL: optimum_on_boundary is not a boolean.")
        return False
    if len(stats.convergence_path) != 4:  # initial center + 3 stages
        print(f"FAIL: convergence_path length mismatch. Got {len(stats.convergence_path)}, expected 4")
        return False
    print("  PASS: Optimization statistics are valid and computed correctly.")

    # 4. Verify Multi-Peak Detection
    print("\nVerifying Multi-Peak Detection...")
    if not hasattr(res, "peaks"):
        print("FAIL: peaks list not found.")
        return False
    print(f"  Found {len(res.peaks)} local peaks within threshold tolerance ({cfg.multi_peak_threshold}).")
    for peak in res.peaks:
        if peak.total_score < stats.best_score - cfg.multi_peak_threshold:
            print(f"FAIL: Peak score {peak.total_score:.4f} is outside the tolerance threshold.")
            return False
    print("  PASS: Multi-peak detection within tolerance successfully verified.")

    # 5. Check Score Improvement
    print("\nVerifying hierarchical score improvements...")
    coarse_s = res.refinement_levels["coarse"]
    ref_s = res.refinement_levels["refinement"]
    sub_s = res.refinement_levels["sub_meter"]
    print(f"  Scores: Coarse={coarse_s:.4f} -> Refined={ref_s:.4f} -> Sub-meter={sub_s:.4f}")
    if ref_s < coarse_s or sub_s < ref_s:
        print(f"FAIL: Optimization score should be non-decreasing across levels! got {coarse_s} -> {ref_s} -> {sub_s}")
        return False
    print("  PASS: Convergence is monotonic across hierarchical levels.")
    
    # 6. Check Reproducibility
    print("\nVerifying reproducibility...")
    res_2 = optimizer.optimize(
        plot_no,
        boundary,
        edge_level,
        transformer,
        patch.transform,
        patch.gray,
        center_dx=0.0,
        center_dy=0.0,
        recorded_area_m2=rec_area,
        map_area_m2=map_area
    )
    if abs(res.best_score - res_2.best_score) > 1e-7:
        print("FAIL: Optimization is not reproducible! Scores differ.")
        return False
    if res.best_candidate.dx != res_2.best_candidate.dx or res.best_candidate.dy != res_2.best_candidate.dy:
        print("FAIL: Optimization is not reproducible! Optimal coordinates differ.")
        return False
    print("  PASS: Optimization results are deterministic and reproducible.")
    
    # 7. Convergence Check (start from different initializations)
    # Start coarse search from (5.0, -5.0) which is a significant offset
    print("\nRunning optimization with initialization offset (5.0, -5.0)...")
    res_offset = optimizer.optimize(
        plot_no,
        boundary,
        edge_level,
        transformer,
        patch.transform,
        patch.gray,
        center_dx=5.0,
        center_dy=-5.0,
        recorded_area_m2=rec_area,
        map_area_m2=map_area
    )
    delta_x = abs(res.best_candidate.dx - res_offset.best_candidate.dx)
    delta_y = abs(res.best_candidate.dy - res_offset.best_candidate.dy)
    print(f"  Optimum starting at (0.0, 0.0): ({res.best_candidate.dx:.3f}, {res.best_candidate.dy:.3f})")
    print(f"  Optimum starting at (5.0, -5.0): ({res_offset.best_candidate.dx:.3f}, {res_offset.best_candidate.dy:.3f})")
    print(f"  Difference: delta_x = {delta_x:.3f} m, delta_y = {delta_y:.3f} m")
    
    # If the offset starting point converges to the same optimum (within step size 0.25m / refinement resolution)
    if delta_x > 0.5 or delta_y > 0.5:
        print("FAIL: Optimizations from different initializations converged to different points!")
        return False
    print("  PASS: Optimizations converge to the same global peak.")
    
    # 8. Check Debug Visualization Saved
    vis_file = debug_dir / "optimizer" / f"plot_{plot_no}_optim_path.png"
    if not vis_file.exists():
        print(f"FAIL: Expected debug trajectory file not found: {vis_file}")
        return False
    print(f"  PASS: Debug trajectory file saved successfully at {vis_file}")
    
    print("\n--- ALL LOCALOPTIMIZER MODULE TESTS PASSED ---")
    return True

def main():
    success = run_optimizer_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
