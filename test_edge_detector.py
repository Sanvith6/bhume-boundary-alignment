import sys
import shutil
from pathlib import Path
import numpy as np
from bhume.config import Config
from bhume.loader import load_village
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector, EdgePatchResult

def run_edge_detector_tests() -> bool:
    print("--- Running EdgeDetector Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_edges")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        scale_pyramids=[1.0, 0.5],
        debug_visualize=True,
        debug_out_dir=str(debug_dir)
    )
    preprocessor = Preprocessor(cfg)
    ms_processor = MultiScaleProcessor(cfg)
    edge_detector = EdgeDetector(cfg)
    
    villages = ["data/village_Malatavadi", "data/village_Vadnerbhairav"]
    methods = ["sobel", "scharr", "canny", "morphological", "combined"]
    
    for v_path in villages:
        village = load_village(v_path)
        plot_no = "1" if "Malatavadi" in v_path else "1145" # Use representative plots
        print(f"\nEvaluating village: {village.slug} | Plot: {plot_no}")
        
        # Crop and generate pyramid
        base_patch = preprocessor.process_plot(village, plot_no)
        ms_patch = ms_processor.generate_pyramid(base_patch, village.plot(plot_no))
        
        for method in methods:
            print(f"  Running detector strategy: '{method}'...")
            try:
                res = edge_detector.detect_pyramid(ms_patch, method=method)
            except Exception as e:
                print(f"FAIL: Exception raised in '{method}': {e}")
                return False
                
            # Verify output type
            if not isinstance(res, EdgePatchResult):
                print(f"FAIL: Result should be EdgePatchResult, got {type(res)}")
                return False
                
            # Verify details for each scale
            for scale in [1.0, 0.5]:
                lvl_res = res.get_level(scale)
                H_s, W_s = lvl_res.edges.shape
                
                # Check bounds & types
                if lvl_res.edges.shape != lvl_res.magnitude.shape:
                    print(f"FAIL: Shape mismatch: edges={lvl_res.edges.shape}, mag={lvl_res.magnitude.shape}")
                    return False
                    
                if lvl_res.edt.shape != (H_s, W_s):
                    print(f"FAIL: EDT shape {lvl_res.edt.shape} must match {(H_s, W_s)}")
                    return False
                    
                # EDT in meters verification: distances should be non-negative
                if lvl_res.edt.min() < 0.0:
                    print(f"FAIL: Distance Transform has negative values!")
                    return False
                    
                # Verification of binary edges
                unique_edges = np.unique(lvl_res.edges)
                if not all(val in (0.0, 1.0) for val in unique_edges):
                    print(f"FAIL: Edge map must be binary [0.0, 1.0], got unique values {unique_edges}")
                    return False
                    
            print(f"    '{method}' check passed.")
            
        # Qualitative report
        print("\nQualitative Observations:")
        # We can extract statistics to compare output qualities
        res_sobel = edge_detector.detect_pyramid(ms_patch, method="sobel").get_level(1.0)
        res_canny = edge_detector.detect_pyramid(ms_patch, method="canny").get_level(1.0)
        res_morph = edge_detector.detect_pyramid(ms_patch, method="morphological").get_level(1.0)
        res_comb = edge_detector.detect_pyramid(ms_patch, method="combined").get_level(1.0)
        
        # Print densities
        print(f"  Sobel Edge Density: {res_sobel.edges.mean() * 100:.2f}% (thick, local thresholds)")
        print(f"  Canny Edge Density: {res_canny.edges.mean() * 100:.2f}% (thinned, thinned contours)")
        print(f"  Morph Gradient Edge Density: {res_morph.edges.mean() * 100:.2f}% (textures captured)")
        print(f"  Combined Edge Density: {res_comb.edges.mean() * 100:.2f}% (fused imagery Canny and boundaries.tif)")
        print(f"  Max EDT Distance (meters): {res_comb.edt.max():.2f} m")
        
        # Verify visual outputs saved
        # The EdgeDetector saves gray, edges, gradient, and edt debug pngs
        vis_base = debug_dir / "edges"
        expected_files = [
            vis_base / f"plot_{plot_no}_scale_1.00_edges.png",
            vis_base / f"plot_{plot_no}_scale_1.00_gradient.png",
            vis_base / f"plot_{plot_no}_scale_1.00_edt.png"
        ]
        for f in expected_files:
            if not f.exists():
                print(f"FAIL: Expected debug file not found: {f}")
                return False
        print("  All debug visualization PNGs saved and verified.")
        
    print("\n--- ALL EDGEDETECTOR MODULE TESTS PASSED ---")
    return True

def main():
    success = run_edge_detector_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
