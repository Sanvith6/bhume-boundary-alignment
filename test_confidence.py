import sys
import shutil
from pathlib import Path
import numpy as np

from bhume.config import Config
from bhume.candidate_generator import CandidateTransformation
from bhume.alignment_scorer import ScoredCandidate
from bhume.optimizer import OptimizationResult, OptimizationStatistics
from bhume.regularizer import RegularizedOptimizationResult
from bhume.confidence import ConfidenceEstimator, ConfidenceResult


def build_mock_inputs(
    dx: float = 1.0,
    dy: float = 1.0,
    best_score: float = 0.8,
    score_gap: float = 0.2,
    score_entropy: float = 1.0,
    area_consistency: float = 0.9,
    neigh_dx: float = 1.0,
    neigh_dy: float = 1.0,
    neighbor_contributions: list = None,
    distance_transform: float = 0.8,
    contour_similarity: float = 0.8,
    gradient_agreement: float = 0.8,
    official_score: float = 0.7,
    candidate_count: int = 100
) -> tuple[OptimizationResult, RegularizedOptimizationResult]:
    """Helper to construct controlled mock inputs for confidence estimation tests."""
    best_cand = CandidateTransformation(dx=dx, dy=dy, search_level="sub_meter")
    
    # Best candidate scored evaluation
    best_scored = ScoredCandidate(
        candidate=best_cand,
        total_score=best_score,
        feature_scores={
            "distance_transform": distance_transform,
            "boundary_hint": 0.8,
            "contour_similarity": contour_similarity,
            "gradient_agreement": gradient_agreement,
            "area_consistency": area_consistency,
            "translation_smoothness": 0.9,
            "shape_preservation": 1.0,
            "neighbor_consistency": 0.9
        }
    )
    
    # Official candidate scored evaluation
    official_cand = CandidateTransformation(dx=0.0, dy=0.0, search_level="coarse")
    official_scored = ScoredCandidate(
        candidate=official_cand,
        total_score=official_score,
        feature_scores={
            "distance_transform": official_score,
            "boundary_hint": 0.7,
            "contour_similarity": 0.7,
            "gradient_agreement": 0.7,
            "area_consistency": 0.9,
            "translation_smoothness": 1.0,
            "shape_preservation": 1.0,
            "neighbor_consistency": 0.9
        }
    )
    
    history = [best_scored, official_scored]
    
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
    
    opt_res = OptimizationResult(
        best_candidate=best_cand,
        best_score=best_score,
        evaluated_candidate_count=candidate_count,
        optimization_history=history,
        refinement_levels={},
        top_k_candidates=[],
        statistics=stats,
        peaks=[]
    )
    
    # Regularized Result setup
    if neighbor_contributions is None:
        neighbor_contributions = [
            {"plot_number": "2", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.8, "score": 0.8},
            {"plot_number": "3", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.8, "score": 0.8},
            {"plot_number": "4", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.8, "score": 0.8}
        ]
        
    reg_res = RegularizedOptimizationResult(
        original_candidate=best_cand,
        regularized_candidate=best_cand,
        applied_shift=(dx, dy),
        local_shift=(dx, dy),
        neighbor_shift=(neigh_dx, neigh_dy),
        blending_factor=1.0,
        neighbor_statistics={
            "neighbor_count": len(neighbor_contributions),
            "neighbor_contributions": neighbor_contributions
        },
        debug_metadata={}
    )
    
    return opt_res, reg_res


def run_confidence_tests() -> bool:
    print("--- Running ConfidenceEstimator Module Verification Tests ---")
    
    debug_dir = Path("debug_test_confidence")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        debug_visualize=True,
        debug_out_dir=str(debug_dir),
        confidence_sigma_improvement=0.05
    )
    
    estimator = ConfidenceEstimator(cfg)
    
    # ----------------------------------------------------
    # Test Case 1: Bounds and Determinism
    # ----------------------------------------------------
    print("\nTest 1: Verifying confidence bounds [0, 1] and determinism...")
    opt_res, reg_res = build_mock_inputs()
    
    res1 = estimator.estimate(opt_res, reg_res)
    res2 = estimator.estimate(opt_res, reg_res)
    
    print(f"  Calibrated Confidence: {res1.confidence:.4f}")
    if res1.confidence < 0.0 or res1.confidence > 1.0:
        print(f"FAIL: Confidence value {res1.confidence} out of [0, 1] bounds!")
        return False
    if res1.confidence != res2.confidence:
        print("FAIL: Confidence estimation is not deterministic across repeated evaluations!")
        return False
    print("  PASS: Confidence is deterministic and within [0, 1].")

    # ----------------------------------------------------
    # Test Case 2: Monotonicity per Signal (Each Signal Tested Independently)
    # ----------------------------------------------------
    print("\nTest 2: Verifying monotonicity for every signal independently...")
    
    # Helper to calculate confidence on a direct signal profile
    def get_conf_for_profile(modifications: dict) -> float:
        # Baseline inputs: set parameters to yield a neutral 0.5 score profile for all signals
        params = {
            "dx": 1.0, "dy": 1.0,
            "best_score": 0.5, "score_gap": 0.05, "score_entropy": 2.3, # yields quality ~0.5, gap ~0.5, entropy ~0.5
            "area_consistency": 0.5,
            "neigh_dx": 1.0, "neigh_dy": 1.0, # yields neighbor_agreement = 1.0
            "neighbor_contributions": [
                {"plot_number": "2", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.5, "score": 0.5},
            ], # yields consensus_strength ~0.16
            "distance_transform": 0.5, "contour_similarity": 0.5, "gradient_agreement": 0.5,
            "official_score": 0.475 # improvement of 0.025 -> S_score_improvement = 0.5
        }
        params.update(modifications)
        
        # Override neighbor agreement distance if requested
        if "neigh_dist" in modifications:
            # Shift neigh_dx to create distance
            params["neigh_dx"] = params["dx"] + modifications["neigh_dist"]
            params.pop("neigh_dist", None)
            
        opt, reg = build_mock_inputs(**params)
        return estimator.estimate(opt, reg).confidence

    # 1. Monotonicity of Optimization Quality
    c_base = get_conf_for_profile({})
    c_inc = get_conf_for_profile({"best_score": 0.7}) # increases S_opt_quality
    print(f"  opt_quality: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_opt_quality.")
        return False
        
    # 2. Monotonicity of Peak Gap
    c_inc = get_conf_for_profile({"score_gap": 0.09}) # increases S_peak_gap
    print(f"  peak_gap: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_peak_gap.")
        return False

    # 3. Monotonicity of Entropy
    c_inc = get_conf_for_profile({"score_entropy": 0.8}) # reduces entropy, increases S_entropy
    print(f"  entropy: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase when entropy decreases.")
        return False

    # 4. Monotonicity of Area Consistency
    c_inc = get_conf_for_profile({"area_consistency": 0.8}) # increases S_area
    print(f"  area_consistency: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_area.")
        return False

    # 5. Monotonicity of Neighbor Agreement
    # Baseline has neigh_dist = 0 -> S_neighbor_agreement = 1.0. We verify it decreases as distance increases.
    c_dist = get_conf_for_profile({"neigh_dist": 2.0}) # adds offset, reduces S_neighbor_agreement
    print(f"  neighbor_agreement: {c_base:.4f} (at 0m) -> {c_dist:.4f} (at 2m)")
    if c_dist >= c_base:
        print("FAIL: final confidence does not decrease when neighbor agreement decreases.")
        return False

    # 6. Monotonicity of Consensus Strength
    c_inc = get_conf_for_profile({
        "neighbor_contributions": [
            {"plot_number": "2", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.9, "score": 0.9},
            {"plot_number": "3", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.9, "score": 0.9},
            {"plot_number": "4", "weight": 1.0, "shift": (1.0, 1.0), "shared_boundary": 10.0, "quality": 0.9, "score": 0.9}
        ]
    }) # increases S_consensus_strength
    print(f"  consensus_strength: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_consensus_strength.")
        return False

    # 7. Monotonicity of Edge Strength
    c_inc = get_conf_for_profile({"distance_transform": 0.8}) # increases S_edge_strength
    print(f"  edge_strength: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_edge_strength.")
        return False

    # 8. Monotonicity of Boundary Coverage
    if estimator.config.confidence_signal_weights.get("boundary_coverage", 0.0) > 0.0:
        c_inc = get_conf_for_profile({"contour_similarity": 0.8}) # increases S_boundary_coverage
        print(f"  boundary_coverage: {c_base:.4f} -> {c_inc:.4f}")
        if c_inc <= c_base:
            print("FAIL: final confidence does not increase with S_boundary_coverage.")
            return False
    else:
        print("  boundary_coverage: skipped monotonicity check (weight is 0)")

    # 9. Monotonicity of Gradient Agreement
    c_inc = get_conf_for_profile({"gradient_agreement": 0.8}) # increases S_gradient_agreement
    print(f"  gradient_agreement: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_gradient_agreement.")
        return False

    # 10. Monotonicity of Score Improvement
    c_inc = get_conf_for_profile({"official_score": 0.45}) # improvement from 0.025 to 0.05, increases S_score_improvement
    print(f"  score_improvement: {c_base:.4f} -> {c_inc:.4f}")
    if c_inc <= c_base:
        print("FAIL: final confidence does not increase with S_score_improvement.")
        return False

    print("  PASS: Monotonicity holds for every signal independently.")

    # ----------------------------------------------------
    # Test Case 3: Critical Zero Suppression
    # ----------------------------------------------------
    print("\nTest 3: Verifying zero-suppression behavior...")
    
    # We verify that only S_edge_strength (critical image evidence) collapses final confidence to 0.0,
    # while non-critical signals (area consistency and score improvement) do not.
    # 1. Zero Edge Strength
    c_zero = get_conf_for_profile({"distance_transform": 0.0})
    print(f"  Zero edge_strength: confidence = {c_zero:.4f}")
    if c_zero > 1e-6:
        print("FAIL: zero edge strength did not suppress confidence to zero.")
        return False
        
    # 2. Zero Area Consistency
    c_zero = get_conf_for_profile({"area_consistency": 0.0})
    print(f"  Zero area_consistency: confidence = {c_zero:.4f}")
    if c_zero < 0.1:
        print("FAIL: zero area consistency should not suppress confidence to zero (softened zero-suppression).")
        return False
        
    # 3. Zero Score Improvement
    c_zero = get_conf_for_profile({"official_score": 0.5}) # best_score=0.5, official_score=0.5 -> improvement = 0.0
    print(f"  Zero score_improvement: confidence = {c_zero:.4f}")
    if c_zero < 0.1:
        print("FAIL: zero score improvement should not suppress confidence to zero (softened zero-suppression).")
        return False

    print("  PASS: Zero suppression behavior verified successfully.")

    # ----------------------------------------------------
    # Test Case 4: Landscape Ambiguity Suppression
    # ----------------------------------------------------
    print("\nTest 4: Verifying that ambiguous optimization landscapes suppress confidence...")
    # Clean unique landscape: large peak gap, low search space entropy
    opt_unique, reg_unique = build_mock_inputs(score_gap=0.3, score_entropy=0.5)
    c_unique = estimator.estimate(opt_unique, reg_unique).confidence
    
    # Ambiguous landscape: tiny peak gap, high search space entropy
    opt_ambig, reg_ambig = build_mock_inputs(score_gap=0.01, score_entropy=4.0)
    c_ambig = estimator.estimate(opt_ambig, reg_ambig).confidence
    
    print(f"  Unique landscape confidence: {c_unique:.4f}")
    print(f"  Ambiguous landscape confidence: {c_ambig:.4f}")
    
    if c_ambig >= c_unique:
        print("FAIL: Ambiguous landscape did not produce lower confidence than unique landscape!")
        return False
    print("  PASS: Ambiguous landscapes correctly penalize confidence.")

    # ----------------------------------------------------
    # Test Case 5: Score Improvement Boundary Correlation
    # ----------------------------------------------------
    print("\nTest 5: Verifying score improvement boundary and correlation...")
    # Substantial improvement: best=0.8, official=0.7 (improvement=0.1)
    opt_sub, reg_sub = build_mock_inputs(best_score=0.8, official_score=0.7)
    c_sub = estimator.estimate(opt_sub, reg_sub).confidence
    
    # Barely any improvement: best=0.8, official=0.798 (improvement=0.002)
    opt_bare, reg_bare = build_mock_inputs(best_score=0.8, official_score=0.798)
    c_bare = estimator.estimate(opt_bare, reg_bare).confidence
    
    # Zero improvement: best=0.8, official=0.8
    opt_zero, reg_zero = build_mock_inputs(best_score=0.8, official_score=0.8)
    c_zero = estimator.estimate(opt_zero, reg_zero).confidence
    
    print(f"  Substantial improvement confidence: {c_sub:.4f}")
    print(f"  Barely any improvement confidence: {c_bare:.4f}")
    print(f"  Zero improvement confidence: {c_zero:.4f}")
    
    if c_bare >= c_sub:
        print("FAIL: Score improvement does not correlate positively with confidence.")
        return False
    if c_zero >= c_bare:
        print("FAIL: Zero improvement confidence should be strictly less than bare improvement confidence.")
        return False
    print("  PASS: Score improvement boundary and positive correlation verified successfully.")

    # ----------------------------------------------------
    # Test Case 6: Visualizations Creation and Outputs
    # ----------------------------------------------------
    print("\nTest 6: Checking debug visualization generation...")
    # Single plot profile
    plot_path = debug_dir / "confidence" / "plot_999_confidence_profile.png"
    opt_vis, reg_vis = build_mock_inputs()
    opt_vis.best_candidate.metadata["plot_number"] = "999"
    estimator.estimate(opt_vis, reg_vis) # triggers visualize_confidence_profile due to cfg.debug_visualize=True
    
    if not plot_path.exists():
        print(f"FAIL: Single plot confidence profile debug image was not saved to: {plot_path}")
        return False
    print(f"  PASS: Individual confidence profile saved at {plot_path}")
    
    # Village histogram
    village_results = {
        "101": estimator.estimate(*build_mock_inputs(best_score=0.8, score_gap=0.2, score_entropy=1.0)),
        "102": estimator.estimate(*build_mock_inputs(best_score=0.7, score_gap=0.1, score_entropy=1.5)),
        "103": estimator.estimate(*build_mock_inputs(best_score=0.9, score_gap=0.3, score_entropy=0.5)),
        "104": estimator.estimate(*build_mock_inputs(best_score=0.4, score_gap=0.01, score_entropy=3.5))
    }
    hist_path = estimator.visualize_village_histogram("test_village", village_results)
    if not hist_path.exists():
        print(f"FAIL: Village confidence histogram debug image was not saved to: {hist_path}")
        return False
    print(f"  PASS: Village confidence histogram saved at {hist_path}")
    
    print("\n--- ALL CONFIDENCEESTIMATOR MODULE TESTS PASSED ---")
    return True


def main():
    success = run_confidence_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
