import sys
import shutil
from pathlib import Path
import numpy as np

from bhume.config import Config
from bhume.loader import load_village, build_neighbor_graph
from bhume.candidate_generator import CandidateTransformation
from bhume.optimizer import OptimizationResult, OptimizationStatistics, ScoredCandidate
from bhume.regularizer import SpatialRegularizer, RegularizedOptimizationResult


def build_mock_result(
    dx: float,
    dy: float,
    best_score: float,
    score_gap: float,
    score_entropy: float,
    candidate_count: int = 100
) -> OptimizationResult:
    """Helper to construct a mock OptimizationResult with specific characteristics."""
    best_cand = CandidateTransformation(dx=dx, dy=dy, search_level="sub_meter")
    
    # Mock statistics
    stats = OptimizationStatistics(
        best_score=best_score,
        second_best_score=best_score - score_gap,
        score_gap=score_gap,
        score_mean=best_score - 0.1,
        score_std=0.05,
        score_entropy=score_entropy,
        number_of_local_maxima=5,
        candidate_count=candidate_count,
        optimum_on_boundary=False,
        convergence_path=[]
    )
    
    return OptimizationResult(
        best_candidate=best_cand,
        best_score=best_score,
        evaluated_candidate_count=candidate_count,
        optimization_history=[],
        refinement_levels={},
        top_k_candidates=[],
        statistics=stats,
        peaks=[]
    )


def run_regularizer_tests() -> bool:
    print("--- Running SpatialRegularizer Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_regularizer")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        debug_visualize=True,
        debug_out_dir=str(debug_dir),
        regularization_iterations=2
    )
    
    # Load village to get real geometries and topology graph
    village = load_village("data/village_Malatavadi")
    graph = build_neighbor_graph(village.plots)
    
    # Let's inspect plot connections
    # We want a test plot with at least 2 neighbors
    test_plot = None
    for pn, neighbors in graph.items():
        if len(neighbors) >= 3:
            test_plot = pn
            break
            
    if test_plot is None:
        print("FAIL: No plot in the village has at least 3 neighbors for tests.")
        return False
        
    neighbor_list = list(graph[test_plot].keys())
    n1, n2, n3 = neighbor_list[0], neighbor_list[1], neighbor_list[2]
    print(f"Selected test plot: {test_plot} with neighbors: {n1}, {n2}, {n3}")
    
    # ----------------------------------------------------
    # Test Case 1: Preservation of High-Quality Local Optima
    # ----------------------------------------------------
    print("\nTest 1: Preserving high-quality local optima...")
    mock_results = {}
    
    # Let test_plot have high quality (Q >= 0.7)
    mock_results[test_plot] = build_mock_result(
        dx=1.0, dy=1.0, best_score=0.95, score_gap=0.5, score_entropy=0.1
    )
    
    # Neighbors have different shifts
    mock_results[n1] = build_mock_result(dx=3.0, dy=3.0, best_score=0.8, score_gap=0.2, score_entropy=1.0)
    mock_results[n2] = build_mock_result(dx=3.0, dy=3.0, best_score=0.8, score_gap=0.2, score_entropy=1.0)
    mock_results[n3] = build_mock_result(dx=3.0, dy=3.0, best_score=0.8, score_gap=0.2, score_entropy=1.0)
    
    # Fill remaining village plots with default values to prevent KeyError
    for pn in graph.keys():
        if pn not in mock_results:
            mock_results[pn] = build_mock_result(dx=0.0, dy=0.0, best_score=0.4, score_gap=0.05, score_entropy=3.0)
            
    regularizer = SpatialRegularizer(cfg)
    reg_res = regularizer.regularize(mock_results, village, graph=graph)
    
    plot_res = reg_res[test_plot]
    print(f"  Local shift: {plot_res.local_shift}")
    print(f"  Applied shift: {plot_res.applied_shift}")
    print(f"  Blending factor (alpha): {plot_res.blending_factor:.2f}")
    
    if plot_res.blending_factor != 1.0:
        print(f"FAIL: Expected alpha=1.0 for high-quality local optimum. Got {plot_res.blending_factor}")
        return False
    if plot_res.applied_shift != (1.0, 1.0):
        print(f"FAIL: Expected shift to remain unchanged. Got {plot_res.applied_shift}")
        return False
    print("  PASS: High-quality local optima are correctly preserved.")
    
    # ----------------------------------------------------
    # Test Case 2: Outlier Correction / Neighbor Consensus
    # ----------------------------------------------------
    print("\nTest 2: Smoothing low-quality outliers toward neighbor consensus...")
    # Now set test_plot to have low quality (Q <= 0.3)
    # best_score=0.2, gap=0.01, entropy=4.5 -> Q ~ 0.1
    mock_results[test_plot] = build_mock_result(
        dx=0.0, dy=0.0, best_score=0.2, score_gap=0.01, score_entropy=4.5
    )
    
    reg_res2 = regularizer.regularize(mock_results, village, graph=graph)
    plot_res2 = reg_res2[test_plot]
    print(f"  Local shift: {plot_res2.local_shift}")
    print(f"  Applied shift: {plot_res2.applied_shift}")
    print(f"  Blending factor (alpha): {plot_res2.blending_factor:.2f}")
    
    if plot_res2.blending_factor >= 0.99:
        print(f"FAIL: Expected alpha < 1.0 for low-quality optimum. Got {plot_res2.blending_factor}")
        return False
    # Shift should have moved from 0.0 towards 3.0
    if plot_res2.applied_shift[0] <= 0.0 or plot_res2.applied_shift[0] >= 3.0:
        print(f"FAIL: Shift didn't interpolate toward neighbor consensus. Got {plot_res2.applied_shift}")
        return False
    print("  PASS: Low-quality local shifts successfully blend toward neighbor consensus.")
    
    # ----------------------------------------------------
    # Test Case 3: Ignored Extreme Neighbor Outliers
    # ----------------------------------------------------
    print("\nTest 3: Rejecting extreme neighbor outliers...")
    # Set all neighbors to have shift (3.0, 3.0) except n3 (the outlier at index 2)
    for idx, n_pn in enumerate(neighbor_list):
        if idx == 2:
            mock_results[n_pn] = build_mock_result(dx=15.0, dy=15.0, best_score=0.8, score_gap=0.2, score_entropy=1.0)
        else:
            mock_results[n_pn] = build_mock_result(dx=3.0, dy=3.0, best_score=0.8, score_gap=0.2, score_entropy=1.0)
    
    reg_res3 = regularizer.regularize(mock_results, village, graph=graph)
    plot_res3 = reg_res3[test_plot]
    print(f"  Neighbor shift: {plot_res3.neighbor_shift}")
    print(f"  Applied shift: {plot_res3.applied_shift}")
    print(f"  Ignored neighbor count: {plot_res3.neighbor_statistics.get('ignored_neighbor_count')}")
    
    # Since n3 was 15.0 and others were 3.0, the median is 3.0. n3's distance is 12.0m, which is > 5.0m.
    # Therefore, n3 should be ignored. The neighbor shift should be close to 3.0 (or smaller due to iterative smoothing propagation), but definitely not pulled towards 15.0.
    if plot_res3.neighbor_shift[0] > 5.0:
        print(f"FAIL: Extreme neighbor outlier was not rejected! Neighbor shift is {plot_res3.neighbor_shift}")
        return False
    if plot_res3.neighbor_statistics.get('ignored_neighbor_count') != 1:
        print(f"FAIL: Expected exactly 1 neighbor to be ignored as outlier.")
        return False
    print("  PASS: Extreme neighbor outliers are correctly rejected.")

    # ----------------------------------------------------
    # Test Case 4: Determinism
    # ----------------------------------------------------
    print("\nTest 4: Verifying determinism...")
    reg_res3_second = regularizer.regularize(mock_results, village, graph=graph)
    if reg_res3[test_plot].applied_shift != reg_res3_second[test_plot].applied_shift:
        print("FAIL: Regularization outputs are not deterministic.")
        return False
    print("  PASS: Regularization outputs are fully reproducible and deterministic.")

    # ----------------------------------------------------
    # Test Case 5: Variance Reduction
    # ----------------------------------------------------
    print("\nTest 5: Verifying reduction of spatial shift variance...")
    # Add random noise to all shifts, but keep quality low so smoothing is active
    np.random.seed(42)
    mock_var_results = {}
    for pn in graph.keys():
        dx = 3.0 + float(np.random.normal(0, 2.0))
        dy = 3.0 + float(np.random.normal(0, 2.0))
        mock_var_results[pn] = build_mock_result(dx=dx, dy=dy, best_score=0.5, score_gap=0.1, score_entropy=1.5)
        
    initial_dxs = [res.best_candidate.dx for res in mock_var_results.values()]
    initial_dys = [res.best_candidate.dy for res in mock_var_results.values()]
    initial_std_x = np.std(initial_dxs)
    initial_std_y = np.std(initial_dys)
    
    reg_var_res = regularizer.regularize(mock_var_results, village, graph=graph)
    reg_dxs = [res.applied_shift[0] for res in reg_var_res.values()]
    reg_dys = [res.applied_shift[1] for res in reg_var_res.values()]
    reg_std_x = np.std(reg_dxs)
    reg_std_y = np.std(reg_dys)
    
    print(f"  Initial standard deviation: X={initial_std_x:.4f}, Y={initial_std_y:.4f}")
    print(f"  Regularized standard deviation: X={reg_std_x:.4f}, Y={reg_std_y:.4f}")
    
    if reg_std_x >= initial_std_x or reg_std_y >= initial_std_y:
        print("FAIL: Regularization should reduce spatial shift variance!")
        return False
    print("  PASS: Spatial shift variance successfully reduced.")

    # ----------------------------------------------------
    # Test Case 6: Debug Visualization
    # ----------------------------------------------------
    print("\nTest 6: Checking debug visualization output...")
    vis_file = debug_dir / "regularizer" / f"{village.slug}_regularizer_grid.png"
    if not vis_file.exists():
        print(f"FAIL: Expected debug map file not found: {vis_file}")
        return False
    print(f"  PASS: Debug regularizer map saved successfully at {vis_file}")
    
    print("\n--- ALL SPATIALREGULARIZER MODULE TESTS PASSED ---")
    return True


def main():
    success = run_regularizer_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
