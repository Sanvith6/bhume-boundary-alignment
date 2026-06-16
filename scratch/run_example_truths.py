import sys
import shutil
import time
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import score as score_predictions

def main():
    print("=== Running Pipeline on 9 Public Example Plots ===")
    
    cfg = Config(
        workers=4,
        cache_enabled=False,
        debug_visualize=False
    )
    coordinator = PipelineCoordinator(cfg)
    
    example_plots = {
        "village_Malatavadi": ['1177', '1763', '1966'],
        "village_Vadnerbhairav": ['1145', '1403', '1476', '1710', '2647', '622']
    }
    
    for name, plots in example_plots.items():
        village_dir = Path("data") / name
        print(f"\nProcessing {name} (plots: {plots})...")
        t0 = time.perf_counter()
        
        # Save existing predictions if any
        original_pred_path = village_dir / "predictions.geojson"
        backup_pred_path = village_dir / "predictions.geojson.backup"
        if original_pred_path.exists():
            shutil.copy(original_pred_path, backup_pred_path)
            
        try:
            # Run coordinator on the example plots
            res = coordinator.run_village(village_dir, plot_numbers=plots)
            elapsed = time.perf_counter() - t0
            print(f"Processed in {elapsed:.2f} seconds.")
            
            # Score results
            village = load_village(village_dir)
            scorecard = score_predictions(original_pred_path, village)
            print()
            print("Scorecard results:")
            print(scorecard)
            
        except Exception as e:
            print(f"Error running pipeline on {name}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Clean up predictions.geojson backup if it exists
            if backup_pred_path.exists():
                backup_pred_path.unlink()

if __name__ == "__main__":
    main()
