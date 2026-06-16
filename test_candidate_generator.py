import sys
import time
import shutil
from pathlib import Path
from bhume.config import Config
from bhume.loader import load_village, CoordinateTransformer
from bhume.preprocessor import Preprocessor
from bhume.candidate_generator import CandidateGenerator, CandidateTransformation

def run_candidate_tests() -> bool:
    print("--- Running CandidateGenerator Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_candidates")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        search_radius_m=20.0,
        search_step_m=1.0,
        fine_search_radius_m=2.0,
        fine_search_step_m=0.25,
        debug_visualize=True,
        debug_out_dir=str(debug_dir)
    )
    preprocessor = Preprocessor(cfg)
    generator = CandidateGenerator(cfg)
    
    # Load Malatavadi
    village = load_village("data/village_Malatavadi")
    plot_no = "1"
    
    # 1. Measure Execution Time
    t_start = time.perf_counter()
    coarse_candidates = generator.generate_coarse(center_dx=5.0, center_dy=-5.0)
    t_coarse = time.perf_counter() - t_start
    
    # Verify coarse candidates count
    # radius=20, step=1 -> 41 points in each direction. 41*41 = 1681 candidates
    expected_coarse_count = 1681
    print(f"  Coarse generation: {len(coarse_candidates)} candidates in {t_coarse * 1000:.2f} ms")
    if len(coarse_candidates) != expected_coarse_count:
        print(f"FAIL: Expected {expected_coarse_count} coarse candidates, got {len(coarse_candidates)}")
        return False
        
    # 2. Check Search Coverage
    dxs = [c.dx for c in coarse_candidates]
    dys = [c.dy for c in coarse_candidates]
    print(f"  Coarse coverage: X limits = [{min(dxs)}, {max(dxs)}], Y limits = [{min(dys)}, {max(dys)}]")
    if min(dxs) != -15.0 or max(dxs) != 25.0: # centered around 5.0
        print(f"FAIL: Coarse search X coverage incorrect! got [{min(dxs)}, {max(dxs)}]")
        return False
    if min(dys) != -25.0 or max(dys) != 15.0: # centered around -5.0
        print(f"FAIL: Coarse search Y coverage incorrect! got [{min(dys)}, {max(dys)}]")
        return False
    print("  PASS: Coarse grid coverage is correct.")
        
    # 3. Test Refinement Neighborhood
    parent = CandidateTransformation(dx=10.0, dy=15.0, search_level="coarse")
    ref_candidates = generator.refine_around(parent)
    # fine_radius = 2.0, fine_step = 0.25 -> 17 points in each direction.
    # 17*17 = 289 points, minus parent itself -> 288 refined candidates.
    expected_ref_count = 288
    print(f"  Refined generation: {len(ref_candidates)} candidates centered at (10, 15)")
    if len(ref_candidates) != expected_ref_count:
        print(f"FAIL: Expected {expected_ref_count} refined candidates, got {len(ref_candidates)}")
        return False
        
    # Check bounds
    ref_dxs = [c.dx for c in ref_candidates]
    ref_dys = [c.dy for c in ref_candidates]
    print(f"  Refinement coverage: X limits = [{min(ref_dxs)}, {max(ref_dxs)}], Y limits = [{min(ref_dys)}, {max(ref_dys)}]")
    if abs(min(ref_dxs) - 8.0) > 1e-5 or abs(max(ref_dxs) - 12.0) > 1e-5:
        print(f"FAIL: Refined search X coverage incorrect!")
        return False
    if abs(min(ref_dys) - 13.0) > 1e-5 or abs(max(ref_dys) - 17.0) > 1e-5:
        print(f"FAIL: Refined search Y coverage incorrect!")
        return False
    print("  PASS: Refinement neighborhood coverage is correct.")
        
    # 4. Check Absence of Duplicates
    # Verify that all coarse candidate IDs are unique
    coarse_ids = [c.candidate_id for c in coarse_candidates]
    if len(coarse_ids) != len(set(coarse_ids)):
        print("FAIL: Duplicate candidate IDs found in coarse list!")
        return False
        
    # Verify no duplicates in refined list
    ref_ids = [c.candidate_id for c in ref_candidates]
    if len(ref_ids) != len(set(ref_ids)):
        print("FAIL: Duplicate candidate IDs found in refined list!")
        return False
        
    # Verify parent is NOT in refined list
    if parent.candidate_id in ref_ids:
        print("FAIL: Parent candidate should not be duplicated in refinement list!")
        return False
    print("  PASS: No duplicates found.")
        
    # 5. Check Reproducibility
    coarse_2 = generator.generate_coarse(center_dx=5.0, center_dy=-5.0)
    coarse_ids_2 = [c.candidate_id for c in coarse_2]
    if coarse_ids != coarse_ids_2:
        print("FAIL: Coarse grid generation is not reproducible!")
        return False
        
    ref_2 = generator.refine_around(parent)
    ref_ids_2 = [c.candidate_id for c in ref_2]
    if ref_ids != ref_ids_2:
        print("FAIL: Refined grid generation is not reproducible!")
        return False
    print("  PASS: Generation is deterministic and reproducible.")
        
    # 6. Verify Debug Visualization
    # Crop patch
    patch = preprocessor.process_plot(village, plot_no)
    # Combine lists to plot both
    all_cands = coarse_candidates + ref_candidates
    vis_path = generator.visualize_search(plot_no, patch, village.plot(plot_no), all_cands)
    print(f"  Debug visualize path returned: {vis_path}")
    if vis_path is None or not vis_path.exists():
        print(f"FAIL: Debug visualization image not created at: {vis_path}")
        return False
    print("  PASS: Debug search grid visualization saved successfully.")
        
    print("\n--- ALL CANDIDATEGENERATOR MODULE TESTS PASSED ---")
    return True

def main():
    success = sc = run_candidate_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
