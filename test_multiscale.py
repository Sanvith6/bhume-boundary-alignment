import sys
import shutil
from pathlib import Path
import numpy as np
from bhume.config import Config
from bhume.loader import load_village
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor, MultiScalePatch

def run_multiscale_tests() -> bool:
    print("--- Running MultiScaleProcessor Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_multiscale")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        scale_pyramids=[1.0, 0.5, 0.25],
        debug_visualize=True,
        debug_out_dir=str(debug_dir)
    )
    preprocessor = Preprocessor(cfg)
    ms_processor = MultiScaleProcessor(cfg)
    
    # Load Malatavadi village
    village_path = "data/village_Malatavadi"
    village = load_village(village_path)
    
    # Use first plot
    plot_no = "1"
    official_geom = village.plot(plot_no)
    
    print(f"Generating base patch for plot {plot_no}...")
    base_patch = preprocessor.process_plot(village, plot_no)
    H_base, W_base = base_patch.image.shape[0], base_patch.image.shape[1]
    print(f"Base patch shape: {H_base}x{W_base}")
    
    print("Generating multi-scale pyramid...")
    try:
        ms_patch = ms_processor.generate_pyramid(base_patch, official_geom)
    except Exception as e:
        print(f"FAIL: Exception raised during pyramid generation: {e}")
        return False
        
    if not isinstance(ms_patch, MultiScalePatch):
        print(f"FAIL: Output is not a MultiScalePatch instance. Got {type(ms_patch)}")
        return False
        
    # Verify levels exist
    for scale in [1.0, 0.5, 0.25]:
        try:
            level = ms_patch.get_level(scale)
        except KeyError:
            print(f"FAIL: Pyramid level for scale {scale} was not generated.")
            return False
            
        print(f"\nVerifying level scale: {scale:.2f}")
        H_s, W_s = level.image.shape[0], level.image.shape[1]
        print(f"  Dimensions: {H_s}x{W_s}")
        
        # Verify dimension ratios
        expected_H = int(round(H_base * scale))
        expected_W = int(round(W_base * scale))
        if H_s != expected_H or W_s != expected_W:
            print(f"FAIL: Expected size {expected_H}x{expected_W}, got {H_s}x{W_s}")
            return False
        print("  Dimension size check passed.")
        
        # Verify boundary hint shape if present
        if level.boundary_hint is not None:
            if level.boundary_hint.shape != (H_s, W_s):
                print(f"FAIL: Boundary hint shape {level.boundary_hint.shape} does not match image {(H_s, W_s)}")
                return False
            unique_vals = np.unique(level.boundary_hint)
            if not all(v in (0.0, 1.0) for v in unique_vals):
                print(f"FAIL: Boundary hint should remain binary, got {unique_vals}")
                return False
            print("  Boundary hint shape and binary value check passed.")
            
        # Verify Transform scaling
        # The determinant of the affine matrix represents pixel area.
        # Since pixel width and height are scaled by 1/s, pixel area is scaled by 1/s^2.
        # So det(level.transform) should be approx det(base_patch.transform) / (s^2)
        det_s = level.transform.determinant
        det_base = base_patch.transform.determinant
        expected_det = det_base / (scale * scale)
        print(f"  Transform det: {det_s:.4f} (Expected: {expected_det:.4f})")
        if abs(det_s - expected_det) > 1e-4:
            print(f"FAIL: Transform determinant mismatch. Diff={abs(det_s - expected_det)}")
            return False
        print("  Transform scaling check passed.")
        
        # Verify reconstruction: the top-left coordinate of pixel (0,0) mapping to CRS
        # should be identical to the base patch because origin is shared.
        x_base, y_base = base_patch.transform * (0, 0)
        x_s, y_s = level.transform * (0, 0)
        print(f"  Origin coordinate: ({x_s:.2f}, {y_s:.2f}) vs Base Origin: ({x_base:.2f}, {y_base:.2f})")
        if abs(x_base - x_s) > 1e-5 or abs(y_base - y_s) > 1e-5:
            print(f"FAIL: Origin of downscaled transform shifted! delta=({abs(x_base - x_s)}, {abs(y_base - y_s)})")
            return False
        print("  Origin coordinate check passed.")
        
        # Verify debug visualization file exists
        expected_vis_file = debug_dir / "multiscale" / f"plot_{plot_no}_scale_{scale:.2f}.png"
        if not expected_vis_file.exists():
            print(f"FAIL: Expected debug visualization file not found at: {expected_vis_file}")
            return False
        print(f"  PASS: Debug file saved successfully at {expected_vis_file}")
        
    print("\n--- ALL MULTISCALEPROCESSOR MODULE TESTS PASSED ---")
    return True

def main():
    success = run_multiscale_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
