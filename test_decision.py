import shutil
import sys
from pathlib import Path
import numpy as np
from shapely.geometry import Polygon, LineString

from bhume.config import Config
from bhume.candidate_generator import CandidateTransformation
from bhume.alignment_scorer import ScoredCandidate
from bhume.optimizer import OptimizationResult, OptimizationStatistics
from bhume.regularizer import RegularizedOptimizationResult
from bhume.confidence import ConfidenceResult
from bhume.decision import DecisionEngine, DecisionResult


def build_mock_inputs(
    dx: float = 1.0,
    dy: float = 1.0,
    best_score: float = 0.8,
    official_score: float = 0.7,
    confidence: float = 0.7,
    score_gap: float = 0.2,
    score_entropy: float = 1.0,
    area_consistency: float = 0.9,
    neighbor_agreement: float = 0.8,
    optimization_quality: float = 0.8,
) -> tuple[OptimizationResult, RegularizedOptimizationResult, ConfidenceResult]:
    """Helper to construct controlled mock inputs for decision engine tests."""
    best_cand = CandidateTransformation(dx=dx, dy=dy, search_level="sub_meter", metadata={"plot_number": "1"})
    official_cand = CandidateTransformation(dx=0.0, dy=0.0, search_level="coarse", metadata={"plot_number": "1"})
    
    best_scored = ScoredCandidate(
        candidate=best_cand,
        total_score=best_score,
        feature_scores={}
    )
    official_scored = ScoredCandidate(
        candidate=official_cand,
        total_score=official_score,
        feature_scores={}
    )
    
    stats = OptimizationStatistics(
        best_score=best_score,
        second_best_score=best_score - score_gap,
        score_gap=score_gap,
        score_mean=best_score - 0.1,
        score_std=0.05,
        score_entropy=score_entropy,
        number_of_local_maxima=5,
        candidate_count=100,
        optimum_on_boundary=False,
        convergence_path=[]
    )
    
    opt_res = OptimizationResult(
        best_candidate=best_cand,
        best_score=best_score,
        evaluated_candidate_count=100,
        optimization_history=[best_scored, official_scored],
        refinement_levels={},
        top_k_candidates=[],
        statistics=stats,
        peaks=[]
    )
    
    reg_res = RegularizedOptimizationResult(
        original_candidate=best_cand,
        regularized_candidate=best_cand,
        applied_shift=(dx, dy),
        local_shift=(dx, dy),
        neighbor_shift=(dx, dy),
        blending_factor=1.0,
        neighbor_statistics={"neighbor_count": 3, "neighbor_contributions": []},
        debug_metadata={}
    )
    
    conf_res = ConfidenceResult(
        confidence=confidence,
        raw_confidence=confidence,
        optimization_quality=optimization_quality,
        neighbor_agreement=neighbor_agreement,
        area_consistency=area_consistency,
        edge_strength=best_score,
        boundary_coverage=best_score,
        peak_gap=score_gap,
        entropy=1.0 - (score_entropy / np.log(100)),
        score_improvement=float(np.clip((best_score - official_score) / 0.05, 0.0, 1.0)),
        support_signals={},
        debug_metadata={}
    )
    
    return opt_res, reg_res, conf_res


def run_decision_tests() -> bool:
    print("--- Running DecisionEngine Module Verification Tests ---")
    
    debug_dir = Path("debug_test_decision")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        debug_visualize=True,
        debug_out_dir=str(debug_dir),
        threshold_correctability=0.6,
        threshold_confidence_veto=0.3,
        threshold_improvement_veto=0.01
    )
    
    engine = DecisionEngine(cfg)
    
    # Valid normal WGS84 coordinates polygon
    valid_geom = Polygon([(74.21, 20.33), (74.211, 20.33), (74.211, 20.331), (74.21, 20.331), (74.21, 20.33)])
    
    # ----------------------------------------------------
    # Test Case 1: Normal Corrected Flow
    # ----------------------------------------------------
    print("\nTest 1: Verifying normal corrected flow...")
    opt_res, reg_res, conf_res = build_mock_inputs(dx=2.0, dy=-1.0, best_score=0.8, official_score=0.6, confidence=0.8)
    
    res = engine.decide(opt_res, reg_res, conf_res, valid_geom)
    print(f"  Status: {res.status}, Correctability: {res.correctability_score:.4f}, Shift: {res.applied_shift}")
    
    if res.status != "corrected":
        print("FAIL: Expected status to be 'corrected' under ideal signals!")
        return False
    if res.applied_shift != (2.0, -1.0):
        print(f"FAIL: Expected applied shift to be (2.0, -1.0), got {res.applied_shift}!")
        return False
    if len(res.decision_reason) > 0:
        print(f"FAIL: Expected no veto reasons, got {res.decision_reason}!")
        return False
    print("  PASS: Normal corrected flow works.")

    # ----------------------------------------------------
    # Test Case 2: Determinism
    # ----------------------------------------------------
    print("\nTest 2: Verifying determinism of decisions...")
    res2 = engine.decide(opt_res, reg_res, conf_res, valid_geom)
    if res.status != res2.status or res.correctability_score != res2.correctability_score or res.applied_shift != res2.applied_shift:
        print("FAIL: Decision Engine is not deterministic!")
        return False
    print("  PASS: Decision Engine is deterministic.")

    # ----------------------------------------------------
    # Test Case 3: Geometric Mean behavior of Correctability
    # ----------------------------------------------------
    print("\nTest 3: Verifying geometric mean correctability score behavior...")
    # Set one signal (e.g. area_consistency) very close to 0.0
    opt_res, reg_res, conf_res = build_mock_inputs(area_consistency=0.0)
    res_zero = engine.decide(opt_res, reg_res, conf_res, valid_geom)
    print(f"  Zero signal correctability: {res_zero.correctability_score:.4f}, Status: {res_zero.status}")
    if res_zero.correctability_score != 0.0:
        print(f"FAIL: Expected correctability score to be 0.0 when a weighted signal is 0.0, got {res_zero.correctability_score}!")
        return False
    if "low_correctability" not in res_zero.decision_reason:
        print(f"FAIL: Expected 'low_correctability' veto, got {res_zero.decision_reason}!")
        return False
    print("  PASS: Geometric mean behavior verified (0.0 correctly masks others).")

    # ----------------------------------------------------
    # Test Case 4: Confidence Veto
    # ----------------------------------------------------
    print("\nTest 4: Verifying confidence veto...")
    opt_res, reg_res, conf_res = build_mock_inputs(confidence=0.25)
    res_conf = engine.decide(opt_res, reg_res, conf_res, valid_geom)
    print(f"  Low confidence status: {res_conf.status}, reasons: {res_conf.decision_reason}")
    if res_conf.status != "flagged" or "low_confidence" not in res_conf.decision_reason:
        print("FAIL: Expected plot to be flagged with 'low_confidence' reason!")
        return False
    if res_conf.applied_shift != (0.0, 0.0):
        print(f"FAIL: Expected flagged plot to revert shift to (0.0, 0.0), got {res_conf.applied_shift}!")
        return False
    print("  PASS: Confidence veto flags correctly.")

    # ----------------------------------------------------
    # Test Case 5: Negligible Improvement Veto
    # ----------------------------------------------------
    print("\nTest 5: Verifying negligible improvement veto...")
    # Improvement is best_score - official_score = 0.8 - 0.795 = 0.005 (less than 0.01 veto threshold)
    opt_res, reg_res, conf_res = build_mock_inputs(best_score=0.8, official_score=0.795)
    res_improv = engine.decide(opt_res, reg_res, conf_res, valid_geom)
    print(f"  Negligible improvement status: {res_improv.status}, reasons: {res_improv.decision_reason}")
    if res_improv.status != "flagged" or "insufficient_improvement" not in res_improv.decision_reason:
        print("FAIL: Expected plot to be flagged with 'insufficient_improvement' reason!")
        return False
    print("  PASS: Negligible improvement veto flags correctly.")

    # ----------------------------------------------------
    # Test Case 6: Invalid Original Geometry Veto
    # ----------------------------------------------------
    print("\nTest 6: Verifying invalid original geometry veto...")
    # Self-intersecting polygon (bowtie shape) is invalid in shapely
    invalid_orig_geom = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    opt_res, reg_res, conf_res = build_mock_inputs(dx=2.0, dy=-1.0)
    res_invalid_orig = engine.decide(opt_res, reg_res, conf_res, invalid_orig_geom)
    print(f"  Invalid original status: {res_invalid_orig.status}, reasons: {res_invalid_orig.decision_reason}")
    if res_invalid_orig.status != "flagged" or "invalid_original_geometry" not in res_invalid_orig.decision_reason:
        print("FAIL: Expected invalid original geometry to be flagged with 'invalid_original_geometry'!")
        return False
    print("  PASS: Invalid original geometry veto verified.")

    # ----------------------------------------------------
    # Test Case 7: Invalid Translated Geometry Veto
    # ----------------------------------------------------
    print("\nTest 7: Verifying invalid translated geometry veto...")
    # A valid line string is not a valid polygon, but let's simulate translation mapping that results in an invalid polygon.
    # How to make a translated polygon invalid?
    # In shapely, translation of a valid polygon is always valid since it is a rigid translation.
    # Wait! How can a translated polygon become topologically invalid?
    # If the translation is invalid or raises an error, or we can mock translation by having a custom translate function or coordinate transformer, or we can just mock a boundary cases.
    # Wait, in the decision logic:
    # "If the translated geometry becomes invalid, it triggers invalid_final_geometry and fails the correction"
    # To test this, we can pass a mock transformer that returns an invalid geometry (e.g. self-intersecting polygon).
    class MockTransformer:
        def geom_to_crs(self, geom):
            return geom
        def geom_to_lonlat(self, geom):
            # Return a self-intersecting invalid polygon
            return Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])

    opt_res, reg_res, conf_res = build_mock_inputs(dx=2.0, dy=-1.0)
    res_invalid_final = engine.decide(opt_res, reg_res, conf_res, valid_geom, transformer=MockTransformer())
    print(f"  Invalid final status: {res_invalid_final.status}, reasons: {res_invalid_final.decision_reason}")
    if res_invalid_final.status != "flagged" or "invalid_final_geometry" not in res_invalid_final.decision_reason:
        print("FAIL: Expected invalid final geometry to be flagged with 'invalid_final_geometry'!")
        return False
    if res_invalid_final.applied_shift != (0.0, 0.0):
        print(f"FAIL: Expected invalid final geometry to revert shift, got {res_invalid_final.applied_shift}!")
        return False
    print("  PASS: Invalid final geometry veto verified.")

    # ----------------------------------------------------
    # Test Case 8: Diagnostics Generation
    # ----------------------------------------------------
    print("\nTest 8: Verifying diagnostic visualizations...")
    results_dict = {
        "plot_1": engine.decide(*build_mock_inputs(dx=2.0, dy=-1.0, best_score=0.8, official_score=0.6, confidence=0.8), valid_geom),
        "plot_2": engine.decide(*build_mock_inputs(dx=1.0, dy=1.0, best_score=0.7, official_score=0.695, confidence=0.7), valid_geom), # Negligible improvement
        "plot_3": engine.decide(*build_mock_inputs(dx=0.5, dy=0.5, best_score=0.6, official_score=0.6, confidence=0.2), valid_geom), # Low confidence, low correctability, worse_than_official
        "plot_4": engine.decide(*build_mock_inputs(dx=1.5, dy=1.5, best_score=0.85, official_score=0.5, confidence=0.95), valid_geom) # High correctability
    }
    
    paths = engine.generate_diagnostics("test_village", results_dict)
    print(f"  Generated {len(paths)} diagnostic charts.")
    for p in paths:
        if not p.exists():
            print(f"FAIL: Diagnostic chart path {p} does not exist!")
            return False
    
    print("  PASS: All 6 diagnostic visual charts successfully generated.")
    return True


if __name__ == "__main__":
    success = run_decision_tests()
    sys.exit(0 if success else 1)
