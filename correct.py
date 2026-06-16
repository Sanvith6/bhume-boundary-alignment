import os
import shutil
from pathlib import Path
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator

def process_village(village_name, plots):
    village_dir = Path(f"data/{village_name}")
    predictions_path = village_dir / "predictions.geojson"
    backup_path = village_dir / "predictions_backup.geojson"
    target_pred_path = village_dir / "pred.geojson"
    
    print(f"\n==============================================")
    print(f"Processing {village_name} for plots: {plots}")
    print(f"==============================================")
    
    # Safely backup the full predictions file if it exists
    if predictions_path.exists():
        shutil.copy2(predictions_path, backup_path)
        
    try:
        cfg = Config(workers="auto")
        coordinator = PipelineCoordinator(cfg)
        
        # Run the pipeline only on the specified plots
        coordinator.run_village(village_dir, plot_numbers=plots)
        
        # The pipeline outputs to predictions.geojson. Rename it to pred.geojson
        if predictions_path.exists():
            shutil.copy2(predictions_path, target_pred_path)
            print(f"\n[SUCCESS] Saved predictions for {len(plots)} plots to {target_pred_path}")
            
    finally:
        # Restore the original full predictions file
        if backup_path.exists():
            shutil.move(backup_path, predictions_path)

if __name__ == "__main__":
    # 1. Process Malatavadi 
    malatavadi_plots = ["1177", "1763", "1966"]
    process_village("village_Malatavadi", malatavadi_plots)
    
    # 2. Process Vadnerbhairav
    vadnerbhairav_plots = ["622", "1145", "1403", "1476", "1710", "2647"]
    process_village("village_Vadnerbhairav", vadnerbhairav_plots)
