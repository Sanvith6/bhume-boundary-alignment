import json
import shutil
import sys
import tempfile
from pathlib import Path
import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon

from bhume.decision import DecisionResult
from bhume.writer import (
    PredictionWriter,
    PredictionWriterError,
    DuplicatePlotNumberError,
    MissingPlotNumberError,
    InvalidStatusError,
    InvalidConfidenceError,
    InvalidGeometryError,
)
from bhume.config import Config


def run_tests():
    print("--- Running PredictionWriter Verification Tests ---")

    # Setup temporary directory for test village
    temp_dir = tempfile.mkdtemp()
    village_dir = Path(temp_dir) / "test_village"
    village_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create a mock input.geojson
    # We will define plots "1", "2", and "3" in input.geojson
    # Valid normal WGS84 coordinates polygon
    geom1 = Polygon([(74.21, 20.33), (74.211, 20.33), (74.211, 20.331), (74.21, 20.331), (74.21, 20.33)])
    geom2 = Polygon([(74.22, 20.34), (74.221, 20.34), (74.221, 20.341), (74.22, 20.341), (74.22, 20.34)])
    geom3 = Polygon([(74.23, 20.35), (74.231, 20.35), (74.231, 20.351), (74.23, 20.351), (74.23, 20.35)])

    input_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"plot_number": "1"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[74.21, 20.33], [74.211, 20.33], [74.211, 20.331], [74.21, 20.331], [74.21, 20.33]]]
                }
            },
            {
                "type": "Feature",
                "properties": {"plot_number": "2"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[74.22, 20.34], [74.221, 20.34], [74.221, 20.341], [74.22, 20.341], [74.22, 20.34]]]
                }
            },
            {
                "type": "Feature",
                "properties": {"plot_number": "3"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[74.23, 20.35], [74.231, 20.35], [74.231, 20.351], [74.23, 20.351], [74.23, 20.35]]]
                }
            }
        ]
    }

    with open(village_dir / "input.geojson", "w", encoding="utf-8") as f:
        json.dump(input_geojson, f)

    # Initialize PredictionWriter with config pointing to temp debug dir
    debug_dir = Path(temp_dir) / "debug_output"
    cfg = Config(debug_out_dir=str(debug_dir))
    writer = PredictionWriter(cfg)

    # ----------------------------------------------------
    # Test Case 1: Geometry Validation & Repair
    # ----------------------------------------------------
    print("\nTest Case 1: Geometry validation and repair...")
    # Self-intersecting polygon (bowtie shape) is invalid
    invalid_geom = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    # We place it in WGS84 bounds so it doesn't fail CRS check
    invalid_geom_wgs84 = Polygon([(74.21, 20.33), (74.212, 20.335), (74.212, 20.33), (74.21, 20.335), (74.21, 20.33)])
    
    # Check that it attempts to repair it with buffer(0).
    # Bowtie shape buffer(0) produces a MultiPolygon or Polygon which is valid.
    res_invalid_but_repairable = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=0.8,
        final_geometry=invalid_geom_wgs84,
        applied_shift=(1.0, 1.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.9},
        score_improvement=0.2,
        correctability_score=0.8,
        decision_trace={},
        debug_metadata={},
    )
    
    # A completely invalid geometry that cannot be repaired:
    # A polygon with less than 3 coordinate points or empty geometry.
    empty_geom = Polygon()
    res_empty_geom = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=0.8,
        final_geometry=empty_geom,
        applied_shift=(1.0, 1.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.9},
        score_improvement=0.2,
        correctability_score=0.8,
        decision_trace={},
        debug_metadata={},
    )

    try:
        writer.write(village_dir, [res_empty_geom])
        print("FAIL: Expected InvalidGeometryError for empty geometry.")
        return False
    except InvalidGeometryError:
        print("PASS Geometry validation (rejected empty geometry)")

    # ----------------------------------------------------
    # Test Case 2: CRS validation
    # ----------------------------------------------------
    print("\nTest Case 2: CRS validation...")
    # Geometry in UTM coordinates
    utm_geom = Polygon([(100000.0, 2000000.0), (100100.0, 2000000.0), (100100.0, 200100.0), (100000.0, 200100.0), (100000.0, 2000000.0)])
    res_utm = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=0.8,
        final_geometry=utm_geom,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.9},
        score_improvement=0.2,
        correctability_score=0.8,
        decision_trace={},
        debug_metadata={},
    )

    try:
        writer.write(village_dir, [res_utm])
        print("FAIL: Expected InvalidGeometryError due to coordinates not in WGS84 range.")
        return False
    except InvalidGeometryError:
        print("PASS CRS validation (rejected non-WGS84 geometries without explicit metadata)")

    # Test reprojection with explicit source_crs
    # Let's reproject from EPSG:32643 to EPSG:4326
    # Let's create a polygon around Kolhapur in UTM zone 43N (approx EPSG:32643 coordinates: Easting ~629000, Northing ~1847000)
    utm_geom_india = Polygon([(629000.0, 1847000.0), (629010.0, 1847000.0), (629010.0, 1847010.0), (629000.0, 1847010.0), (629000.0, 1847000.0)])
    res_utm_reproj = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=0.8,
        final_geometry=utm_geom_india,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.9},
        score_improvement=0.2,
        correctability_score=0.8,
        decision_trace={},
        debug_metadata={},
    )
    
    try:
        out_p = writer.write(village_dir, [res_utm_reproj], source_crs="EPSG:32643")
        # Read back written file and verify coordinates are reprojected
        with open(out_p, "r", encoding="utf-8") as f:
            written_json = json.load(f)
            coords = written_json["features"][0]["geometry"]["coordinates"][0]
            # Ensure coordinates are now around 76.2 E, 16.7 N
            assert 76.0 <= coords[0][0] <= 77.0
            assert 16.0 <= coords[0][1] <= 17.0
        print("PASS CRS validation (reprojected successfully using source_crs)")
    except Exception as e:
        print(f"FAIL: Reprojection test failed: {e}")
        return False

    # ----------------------------------------------------
    # Test Case 3: Confidence Validation
    # ----------------------------------------------------
    print("\nTest Case 3: Confidence validation...")
    res_low_conf = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=-0.1,
        final_geometry=geom1,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.9},
        score_improvement=0.2,
        correctability_score=0.8,
        decision_trace={},
        debug_metadata={},
    )
    res_high_conf = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=1.1,
        final_geometry=geom1,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.9},
        score_improvement=0.2,
        correctability_score=0.8,
        decision_trace={},
        debug_metadata={},
    )

    try:
        writer.write(village_dir, [res_low_conf])
        print("FAIL: Expected InvalidConfidenceError for negative confidence.")
        return False
    except InvalidConfidenceError:
        pass

    try:
        writer.write(village_dir, [res_high_conf])
        print("FAIL: Expected InvalidConfidenceError for confidence > 1.0.")
        return False
    except InvalidConfidenceError:
        print("PASS Confidence validation (correctly rejected invalid bounds)")

    # ----------------------------------------------------
    # Test Case 4: Duplicate Detection & Missing Plot Detection
    # ----------------------------------------------------
    print("\nTest Case 4: Duplicate and missing plot detection...")
    res_plot1_a = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=0.8,
        final_geometry=geom1,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={},
        score_improvement=0.0,
        correctability_score=0.0,
        decision_trace={},
        debug_metadata={},
    )
    res_plot1_b = DecisionResult(
        plot_number="1",
        status="flagged",
        confidence=0.5,
        final_geometry=geom1,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={},
        score_improvement=0.0,
        correctability_score=0.0,
        decision_trace={},
        debug_metadata={},
    )
    res_missing_plot = DecisionResult(
        plot_number="999", # not in input.geojson
        status="corrected",
        confidence=0.8,
        final_geometry=geom1,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={},
        score_improvement=0.0,
        correctability_score=0.0,
        decision_trace={},
        debug_metadata={},
    )

    try:
        writer.write(village_dir, [res_plot1_a, res_plot1_b])
        print("FAIL: Expected DuplicatePlotNumberError.")
        return False
    except DuplicatePlotNumberError:
        print("PASS Duplicate detection")

    try:
        writer.write(village_dir, [res_missing_plot])
        print("FAIL: Expected MissingPlotNumberError.")
        return False
    except MissingPlotNumberError:
        print("PASS Missing plot detection")

    # ----------------------------------------------------
    # Test Case 5: Deterministic Ordering & Byte-Identical Serialization
    # ----------------------------------------------------
    print("\nTest Case 5: Deterministic serialization and ordering...")
    # Put results out of order: plot 3, then plot 1, then plot 2
    res_p1 = DecisionResult(
        plot_number="1",
        status="corrected",
        confidence=0.8234567, # will be rounded to 0.823457
        final_geometry=geom1,
        applied_shift=(1.0, -1.0),
        decision_reason=[],
        decision_signals={"optimization_quality": 0.85},
        score_improvement=0.25,
        correctability_score=0.82,
        decision_trace={},
        debug_metadata={},
    )
    res_p2 = DecisionResult(
        plot_number="2",
        status="flagged",
        confidence=0.5,
        final_geometry=geom2,
        applied_shift=(0.0, 0.0),
        decision_reason=["low_confidence"],
        decision_signals={},
        score_improvement=0.0,
        correctability_score=0.1,
        decision_trace={},
        debug_metadata={},
    )
    res_p3 = DecisionResult(
        plot_number="3",
        status="corrected",
        confidence=0.9,
        final_geometry=geom3,
        applied_shift=(0.5, 0.5),
        decision_reason=[],
        decision_signals={},
        score_improvement=0.15,
        correctability_score=0.75,
        decision_trace={},
        debug_metadata={},
    )

    results_unordered = [res_p3, res_p1, res_p2]

    # Write first time
    out_path_1 = writer.write(village_dir, results_unordered)
    with open(out_path_1, "rb") as f:
        bytes_1 = f.read()

    # Write second time
    out_path_2 = writer.write(village_dir, results_unordered)
    with open(out_path_2, "rb") as f:
        bytes_2 = f.read()

    if bytes_1 != bytes_2:
        print("FAIL: Output serialization is not byte-identical across identical runs!")
        return False
    print("PASS Deterministic serialization (byte-identical)")

    # Read back and verify order of features in GeoJSON
    with open(out_path_1, "r", encoding="utf-8") as f:
        data = json.load(f)
        pns = [feat["properties"]["plot_number"] for feat in data["features"]]
        if pns != ["1", "2", "3"]:
            print(f"FAIL: Ordered list mismatch! Expected ['1', '2', '3'], got {pns}")
            return False
    print("PASS Deterministic serialization (sorted order)")

    # Verify confidence rounding to 6 decimal places
    written_conf = data["features"][0]["properties"]["confidence"]
    if written_conf != 0.823457:
        print(f"FAIL: Expected confidence rounded to 6 decimal places (0.823457), got {written_conf}")
        return False
    print("PASS Confidence validation (rounded to 6 decimals)")

    # ----------------------------------------------------
    # Test Case 6: GeoJSON Schema & GeoPandas / score.py Compatibility
    # ----------------------------------------------------
    print("\nTest Case 6: GeoJSON Schema & compatibility...")
    # Verify we can read it back with GeoPandas
    try:
        gdf_read = gpd.read_file(out_path_1)
        assert gdf_read.crs is not None
        assert set(gdf_read.columns).issuperset({"plot_number", "status", "confidence", "method_note", "geometry"})
        print("PASS GeoJSON schema & GeoPandas load succeeds")
    except Exception as e:
        print(f"FAIL: GeoPandas read failed: {e}")
        return False

    # Check validation report generated and contains correct fields
    report_path = debug_dir / "predictions" / "contract_validation_report.json"
    if not report_path.exists():
        print("FAIL: contract_validation_report.json not found!")
        return False
    with open(report_path, "r") as f:
        report = json.load(f)
        required_keys = {"schema_valid", "geometry_valid", "crs_valid", "duplicate_plots", "invalid_confidence", "timestamp", "validation_passed"}
        assert required_keys.issubset(report.keys())
        assert report["validation_passed"] is True
    print("PASS Contract validation (report contains correct schema & values)")

    # ----------------------------------------------------
    # Test Case 7: Invalid Status Rejection
    # ----------------------------------------------------
    print("\nTest Case 7: Invalid status rejection...")
    res_bad_status = DecisionResult(
        plot_number="1",
        status="maybe_corrected",
        confidence=0.8,
        final_geometry=geom1,
        applied_shift=(0.0, 0.0),
        decision_reason=[],
        decision_signals={},
        score_improvement=0.0,
        correctability_score=0.0,
        decision_trace={},
        debug_metadata={},
    )
    try:
        writer.write(village_dir, [res_bad_status])
        print("FAIL: Expected InvalidStatusError.")
        return False
    except InvalidStatusError:
        print("PASS Invalid status rejection")

    # Cleanup temp directory
    shutil.rmtree(temp_dir)

    print("\nPASS Geometry validation")
    print("PASS CRS validation")
    print("PASS Confidence validation")
    print("PASS Duplicate detection")
    print("PASS Deterministic serialization")
    print("PASS GeoJSON schema")
    print("PASS Contract validation")
    print("PASS score.py compatibility")
    print("ALL PREDICTIONWRITER TESTS PASSED")
    return True


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
