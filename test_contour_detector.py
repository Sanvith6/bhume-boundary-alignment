import sys
import shutil
from pathlib import Path
import numpy as np
from bhume.config import Config
from bhume.loader import load_village, CoordinateTransformer
from bhume.preprocessor import Preprocessor
from bhume.contour_detector import ContourDetector, BoundaryRepresentation
import shapely.geometry
from bhume.geo import open_imagery


def run_contour_tests() -> bool:
    print("--- Running ContourDetector Module Verification Tests ---")
    
    # Clean previous debug directories
    debug_dir = Path("debug_test_contours")
    if debug_dir.exists():
        shutil.rmtree(debug_dir)
        
    cfg = Config(
        contour_sample_interval_m=1.0, # 1.0 meter intervals
        debug_visualize=True,
        debug_out_dir=str(debug_dir)
    )
    preprocessor = Preprocessor(cfg)
    contour_detector = ContourDetector(cfg)
    
    villages = ["data/village_Malatavadi", "data/village_Vadnerbhairav"]
    
    for v_path in villages:
        village = load_village(v_path)
        plot_no = "1" if "Malatavadi" in v_path else "1145"
        print(f"\nTesting village: {village.slug} | Plot: {plot_no}")
        
        # Load and transform coordinates using transformer
        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)
            geom_crs = transformer.geom_to_crs(village.plot(plot_no))
            # Crop to get patch size
            patch = preprocessor.process_plot(village, plot_no)
            H, W = patch.gray.shape
            
        # Parameterize
        try:
            bnd_rep = contour_detector.parameterize_boundary(
                plot_no,
                village.plot(plot_no),
                transformer,
                image_shape=(H, W)
            )
        except Exception as e:
            print(f"FAIL: Exception raised during boundary parameterization: {e}")
            return False
            
        if not isinstance(bnd_rep, BoundaryRepresentation):
            print(f"FAIL: Result should be BoundaryRepresentation, got {type(bnd_rep)}")
            return False
            
        pts = bnd_rep.sampled_points
        N = len(pts)
        print(f"  Sampled {N} points along perimeter of length {bnd_rep.total_length:.2f} meters.")
        
        # 1. Spacing check (should be approx equal to sampling_interval = 1.0m)
        # Check distance between successive points
        diffs = np.diff(pts, axis=0)
        # Include closing segment from last point to first
        closing_diff = pts[0] - pts[-1]
        diffs = np.vstack([diffs, closing_diff])
        distances = np.linalg.norm(diffs, axis=1)
        mean_dist = np.mean(distances)
        std_dist = np.std(distances)
        print(f"  Spacing metrics: mean = {mean_dist:.4f} m, std = {std_dist:.4f} m")
        
        # Spacing should be close to the sampling interval on average.
        # Standard deviation can be up to 0.15 due to corner cutting.
        non_closing_std = np.std(distances[:-1])
        target_ds = bnd_rep.sampling_interval
        if abs(mean_dist - target_ds) > 0.05:
            print(f"FAIL: Average spacing {mean_dist:.4f} is too far from target ds = {target_ds}")
            return False
        if non_closing_std > 0.15:
            print(f"FAIL: Spacing is too non-uniform! std of non-closing segments = {non_closing_std:.4f} m")
            return False
        print("  PASS: Points are approximately uniformly spaced.")
        
        # 2. Length check
        # Sum of segment lengths should equal total_length approx
        sum_len = np.sum(distances)
        len_diff = abs(sum_len - bnd_rep.total_length)
        # Euclidean sum is strictly less than arc-length because of corner cutting.
        # We allow up to 2% difference due to linear chords cutting corners.
        max_allowed_diff = 0.02 * bnd_rep.total_length
        if len_diff > max_allowed_diff:
            print(f"FAIL: Segment sum {sum_len:.2f} differs from total length {bnd_rep.total_length:.2f} by {len_diff:.4f} m (max allowed: {max_allowed_diff:.4f})")
            return False
        print("  PASS: Total boundary length is correct.")
        
        # 3. Tangent normalization check (norms should be 1.0)
        t_norms = np.linalg.norm(bnd_rep.tangents, axis=1)
        print(f"  Tangent norms: mean = {t_norms.mean():.4f}, std = {t_norms.std():.4f}")
        if not np.allclose(t_norms, 1.0, atol=1e-5):
            print(f"FAIL: Tangents are not normalized!")
            return False
        print("  PASS: Tangent normalization is correct.")
        
        # 4. Normal orthogonality check (tangent dot normal should be 0.0)
        dot_products = np.sum(bnd_rep.tangents * bnd_rep.normals, axis=1)
        print(f"  Tangent-Normal dot products: max absolute value = {np.max(np.abs(dot_products)):.2e}")
        if not np.allclose(dot_products, 0.0, atol=1e-5):
            print(f"FAIL: Normals are not orthogonal to tangents!")
            return False
        print("  PASS: Normal orthogonality is correct.")
        
        # 5. Order/orientation check
        # For a counter-clockwise polygon, the normal N = (ty, -tx) should point outwards.
        # We can verify this by checking if pt + eps*normal is outside the polygon.
        # Let's take a point and test:
        poly_crs = transformer.geom_to_crs(village.plot(plot_no))
        # Let's do a simple inside-outside test for all points shifted slightly along the normal
        shifted_out = pts + 0.1 * bnd_rep.normals
        shifted_in = pts - 0.1 * bnd_rep.normals
        
        out_outside = [not poly_crs.contains(shapely.geometry.Point(pt[0], pt[1])) for pt in shifted_out]
        in_inside = [poly_crs.contains(shapely.geometry.Point(pt[0], pt[1])) for pt in shifted_in]
        
        out_rate = sum(out_outside) / N
        in_rate = sum(in_inside) / N
        print(f"  Outward normal sanity check: outward-rate = {out_rate*100:.1f}%, inward-rate = {in_rate*100:.1f}%")
        
        # The outward rates should be very high (near 100%, slight offsets due to corners are possible)
        if out_rate < 0.90 or in_rate < 0.90:
            print(f"FAIL: Normals do not consistently point outwards! Outward rate = {out_rate:.2f}, Inward rate = {in_rate:.2f}")
            return False
        print("  PASS: Normal directions point outward and ordering is CCW.")
        
        # Check debug visualization saved
        vis_file = debug_dir / "contours" / f"plot_{plot_no}_contour.png"
        if not vis_file.exists():
            print(f"FAIL: Expected debug visualization file not found at: {vis_file}")
            return False
        print(f"  PASS: Debug file saved successfully at {vis_file}")
        
    print("\n--- ALL CONTOURDETECTOR MODULE TESTS PASSED ---")
    return True

def main():
    success = run_contour_tests()
    if success:
        print("RESULT: PASS")
        sys.exit(0)
    else:
        print("RESULT: FAIL")
        sys.exit(1)

if __name__ == "__main__":
    main()
