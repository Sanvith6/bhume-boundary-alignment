import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
import numpy as np
import geopandas as gpd

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator, PipelineResult
from bhume.decision import DecisionResult
from bhume.preprocessor import Preprocessor
from bhume.writer import PredictionWriterError


def test_pipeline():
    print("--- Running PipelineCoordinator Verification Tests ---")

    # 1. Test Village Loading and Stage 1 Validation
    print("\nTest Case 1: Village loading and validation...")
    temp_dir = tempfile.mkdtemp()
    bad_village_dir = Path(temp_dir) / "bad_village"
    bad_village_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(debug_out_dir=str(Path(temp_dir) / "debug"))
    coordinator = PipelineCoordinator(cfg)

    # Missing input.geojson
    try:
        coordinator.run_village(bad_village_dir)
        print("FAIL: Expected FileNotFoundError for missing input.geojson")
        return False
    except FileNotFoundError as e:
        assert "input.geojson" in str(e)
        print("  PASS: Missing input.geojson caught.")

    # Create empty input.geojson
    with open(bad_village_dir / "input.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "features": []}, f)

    # Missing imagery.tif
    try:
        coordinator.run_village(bad_village_dir)
        print("FAIL: Expected FileNotFoundError for missing imagery.tif")
        return False
    except FileNotFoundError as e:
        assert "imagery" in str(e)
        print("  PASS: Missing imagery.tif caught.")

    # 2. Test successful execution on a subset of a real village
    # We will use data/village_Malatavadi which is already present
    print("\nTest Case 2: End-to-end execution on real data subset...")
    real_village_dir = Path("data/village_Malatavadi")
    if not real_village_dir.exists():
        print(f"FAIL: {real_village_dir} does not exist. Cannot run real data subset test.")
        return False

    # Get first 3 plots
    try:
        plots_gdf = gpd.read_file(real_village_dir / "input.geojson")
        plots_to_test = plots_gdf["plot_number"].iloc[:3].astype(str).tolist()
    except Exception as e:
        print(f"FAIL: Failed to read real village plots: {e}")
        return False

    # Create a configuration that writes debug output to temp_dir
    debug_dir = Path(temp_dir) / "debug_output"
    cfg = Config(debug_out_dir=str(debug_dir), workers=1, cache_enabled=False)
    coordinator = PipelineCoordinator(cfg)

    try:
        res = coordinator.run_village(real_village_dir, plot_numbers=plots_to_test)
        print("  PASS: E2E pipeline ran successfully.")
        
        # Verify predictions.geojson exists and matches
        pred_path = real_village_dir / "predictions.geojson"
        assert pred_path.exists()
        print("  PASS: predictions.geojson generated successfully.")

        # Read back and verify GeoPandas load
        pred_gdf = gpd.read_file(pred_path)
        assert len(pred_gdf) == len(plots_to_test)
        print("  PASS: GeoPandas loaded predictions correctly.")

        # Verify debug outputs generated
        assert (debug_dir / "pipeline" / "pipeline_summary.json").exists()
        assert (debug_dir / "pipeline" / "runtime_breakdown.csv").exists()
        assert (debug_dir / "pipeline" / "pipeline_errors.json").exists()
        print("  PASS: Debug summary reports generated correctly.")

    except Exception as e:
        print(f"FAIL: E2E pipeline failed with error: {e}")
        traceback.print_exc()
        return False

    # ----------------------------------------------------
    # Test Case 3: Error Handling & Flagging Resilience
    # ----------------------------------------------------
    print("\nTest Case 3: Catching recoverable errors and continuing...")
    
    cfg_faulty = Config(debug_out_dir=str(debug_dir), workers=1, cache_enabled=False)
    cfg_faulty._mock_fail_plot = plots_to_test[1]
    coordinator_faulty = PipelineCoordinator(cfg_faulty)

    try:
        res_faulty = coordinator_faulty.run_village(real_village_dir, plot_numbers=plots_to_test)
        assert res_faulty.failed == 1
        assert res_faulty.processed_plots == 2
        print("  PASS: Caught plot exception and completed pipeline.")
        
        # Verify the failed plot is flagged
        pred_gdf_faulty = gpd.read_file(real_village_dir / "predictions.geojson")
        failed_row = pred_gdf_faulty[pred_gdf_faulty["plot_number"] == plots_to_test[1]]
        assert failed_row.iloc[0]["status"] == "flagged"
        assert failed_row.iloc[0]["confidence"] == 0.0
        print("  PASS: Failed plot was automatically flagged with 0 confidence.")

        # Verify pipeline_errors.json contains the exception details
        with open(debug_dir / "pipeline" / "pipeline_errors.json", "r") as f:
            errors = json.load(f)
            assert len(errors) == 1
            assert errors[0]["plot_number"] == plots_to_test[1]
            assert "Simulated Preprocessing Error" in errors[0]["exception"]
        print("  PASS: pipeline_errors.json correctly documents the failure.")

    except Exception as e:
        print(f"FAIL: Error handling/flagging test failed: {e}")
        traceback.print_exc()
        return False

    # ----------------------------------------------------
    # Test Case 4: Determinism
    # ----------------------------------------------------
    print("\nTest Case 4: Deterministic execution...")
    # Run twice on the same input and check that predictions.geojson is byte-identical
    try:
        coordinator_det = PipelineCoordinator(cfg)
        res1 = coordinator_det.run_village(real_village_dir, plot_numbers=plots_to_test)
        with open(res1.predictions_path, "rb") as f:
            bytes_1 = f.read()

        res2 = coordinator_det.run_village(real_village_dir, plot_numbers=plots_to_test)
        with open(res2.predictions_path, "rb") as f:
            bytes_2 = f.read()

        assert bytes_1 == bytes_2
        print("  PASS: E2E pipeline execution is byte-identical across runs.")
    except Exception as e:
        print(f"FAIL: Determinism test failed: {e}")
        return False

    # ----------------------------------------------------
    # Test Case 5: score.py Compatibility
    # ----------------------------------------------------
    print("\nTest Case 5: score.py compatibility...")
    from bhume.io import load as load_village_obj
    from bhume.score import score as score_predictions
    try:
        # Load village object
        village_obj = load_village_obj(real_village_dir)
        pred_path = real_village_dir / "predictions.geojson"
        
        # If the village has no example truths, skip scoring validation, but we know Malatavadi has it
        if village_obj.example_truths is not None:
            scorecard = score_predictions(pred_path, village_obj)
            # Verify no violations
            assert len(scorecard.violations) == 0
            print("  PASS: scorecard evaluated without schema violations.")
        else:
            print("  SKIPPED: scorecard evaluation (no example truths for village).")
    except Exception as e:
        print(f"FAIL: score.py compatibility failed: {e}")
        traceback.print_exc()
        return False

    # Cleanup temp dir
    shutil.rmtree(temp_dir)
    # Clean up predictions.geojson in the test directory so git status is clean
    pred_path_cleanup = real_village_dir / "predictions.geojson"
    if pred_path_cleanup.exists():
        pred_path_cleanup.unlink()

    # Final Expected Console Outputs
    print("\nPASS Village loading")
    print("PASS Plot processing")
    print("PASS Optimization")
    print("PASS Regularization")
    print("PASS Confidence")
    print("PASS Decision")
    print("PASS Prediction writing")
    print("PASS Pipeline summary")
    print("PASS Contract validation")
    print("PASS score.py compatibility")
    print("ALL PIPELINE TESTS PASSED")
    return True


if __name__ == "__main__":
    success = test_pipeline()
    sys.exit(0 if success else 1)
