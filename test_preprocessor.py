import sys
import shutil
from pathlib import Path
import numpy as np
from bhume.config import Config
from bhume.loader import load_village
from bhume.preprocessor import Preprocessor, PreprocessedPatch

def run_preprocessor_tests() -> bool:
    print("--- Running Preprocessor Module Verification Tests ---")
    
    # 1. Clean previous debug directories
    debug_dir = Path("debug_test_preprocessor")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(debug_visualize=True, debug_out_dir=str(debug_dir))
    preprocessor = Preprocessor(cfg)
    
    # Load Malatavadi village
    village_path = "data/village_Malatavadi"
    village = load_village(village_path)
    
    # Crop the first 3 plots
    test_plots = list(village.plots.index[:3])
    print(f"Testing preprocessor on plots: {test_plots}")
    
    for plot_no in test_plots:
        print(f"\nProcessing plot: {plot_no}")
        try:
            patch = preprocessor.process_plot(village, plot_no)
        except Exception as e:
            print(f"FAIL: Exception raised during processing: {e}")
            return False
            
        # Verify instance type
        if not isinstance(patch, PreprocessedPatch):
            print(f"FAIL: Output is not a PreprocessedPatch instance. Got {type(patch)}")
            return False
            
        # Verify imagery shapes and boundaries
        H, W, C = patch.image.shape
        print(f"  Imagery Patch size: H={H}, W={W}, Channels={C}")
        if C != 3:
            print(f"FAIL: Image should have 3 channels, got {C}")
            return False
            
        if patch.image.min() < 0.0 or patch.image.max() > 1.0:
            print(f"FAIL: Normalization failed. Image values min={patch.image.min()}, max={patch.image.max()} outside [0, 1].")
            return False
            
        # Verify grayscale
        print(f"  Grayscale Patch size: {patch.gray.shape}")
        if patch.gray.shape != (H, W):
            print(f"FAIL: Grayscale shape {patch.gray.shape} must match imagery {(H, W)}")
            return False
            
        if patch.gray.min() < 0.0 or patch.gray.max() > 1.0:
            print(f"FAIL: Grayscale values min={patch.gray.min()}, max={patch.gray.max()} outside [0, 1]")
            return False
            
        # Verify boundary hints
        if patch.boundary_hint is not None:
            print(f"  Boundary Hint Patch size: {patch.boundary_hint.shape}")
            if patch.boundary_hint.shape != (H, W):
                print(f"FAIL: Boundary hint shape {patch.boundary_hint.shape} does not match imagery {(H, W)}")
                return False
            unique_vals = np.unique(patch.boundary_hint)
            print(f"  Boundary unique values: {unique_vals}")
            if not all(val in (0.0, 1.0) for val in unique_vals):
                print(f"FAIL: Boundary hint must contain only binary [0.0, 1.0] values, got {unique_vals}")
                return False
        else:
            print("  Boundary Hint Patch: None (boundaries.tif not found)")
            
        # Verify coordinate transformation variables
        print(f"  Patch bounds: {patch.bounds}")
        print(f"  Patch transform:\n{patch.transform}")
        
        # Verify debug visualization file is saved
        expected_vis_file = debug_dir / "preprocessing" / f"plot_{plot_no}_preprocess.png"
        if not expected_vis_file.exists():
            print(f"FAIL: Expected debug visualization file not found at: {expected_vis_file}")
            return False
        print(f"  PASS: Debug file saved successfully at {expected_vis_file}")
        
    print("\n--- ALL PREPROCESSOR MODULE TESTS PASSED ---")
    return True

def main():
    success = run_preprocessor_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
