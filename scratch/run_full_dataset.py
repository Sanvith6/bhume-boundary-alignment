import json
import shutil
import sys
import os
import logging
from pathlib import Path

sys.path.insert(0, ".")
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator

def run_village_full(village_dir_path):
    village_name = village_dir_path.name
    print(f"\n{'='*60}")
    print(f"Running FULL DATASET for {village_name} on CPU (Multiprocessing)")
    print(f"{'='*60}")
    
    # Configure and run
    # Safely throttle to 6 workers as requested to prevent CPU freeze on massive dataset
    config = Config(workers=6)
    coordinator = PipelineCoordinator(config)
    
    # Do not pass plot_numbers, so it runs on ALL plots in the village
    try:
        result = coordinator.run_village(village_dir_path)
    except Exception as e:
        print(f"Failed to process {village_name}: {e}")
        return
    
    # Ensure predictions are named correctly
    pred_path = village_dir_path / "predictions.geojson"
    target_path = village_dir_path / "gpupred.geojson"
    
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
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    
    data_dir = Path("data")
    for village_dir in data_dir.iterdir():
        if village_dir.is_dir() and not village_dir.name.startswith(".") and "Vadnerbhairav" in village_dir.name:
            run_village_full(village_dir)

    print("\nALL VILLAGES COMPLETED!")
