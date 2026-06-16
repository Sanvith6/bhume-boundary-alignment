import json
import shutil
import sys
sys.path.insert(0, ".")
from pathlib import Path
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator

def run_9_plots(village_name):
    print(f"\n{'='*60}")
    print(f"Running 9 plots for {village_name} on CPU (Multiprocessing)")
    print(f"{'='*60}")
    
    village_dir = Path(f"data/{village_name}")
    truths_path = village_dir / "example_truths.geojson"
    
    # Extract plot numbers from example_truths.geojson
    with open(truths_path) as f:
        truths = json.load(f)
    
    plot_numbers = [str(f["properties"]["plot_number"]) for f in truths["features"]]
    print(f"Extracted plot numbers: {plot_numbers}")
    
    # Configure and run
    # Set workers="auto" to use all available CPU cores!
    config = Config(workers="auto")
    coordinator = PipelineCoordinator(config)
    
    result = coordinator.run_village(village_dir, plot_numbers=plot_numbers)
    
    # Rename predictions.geojson to gpupred.geojson (keeping the user's requested filename)
    pred_path = village_dir / "predictions.geojson"
    target_path = village_dir / "gpupred.geojson"
    
    if pred_path.exists():
        shutil.copy2(pred_path, target_path)
        print(f"Successfully created {target_path}")
    else:
        print(f"ERROR: {pred_path} was not created!")
        
    print(f"\n--- {village_name} Results ---")
    print(f"Total plots:      {result.total_plots}")
    print(f"Processed:        {result.processed_plots}")
    print(f"Corrected:        {result.corrected}")
    print(f"Flagged:          {result.flagged}")
    print(f"Failed:           {result.failed}")

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    
    run_9_plots("village_Malatavadi")
    run_9_plots("village_Vadnerbhairav")
