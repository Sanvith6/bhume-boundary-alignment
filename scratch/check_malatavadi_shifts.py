import sys
import numpy as np
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village
from bhume.coordinator import PipelineCoordinator

def check_shifts():
    cfg = Config(workers=1, cache_enabled=False, debug_visualize=False)
    coordinator = PipelineCoordinator(cfg)
    
    village_dir = Path("data/village_Malatavadi")
    village = load_village(village_dir)
    
    # We want to run Pass 1 and get all shifts
    # (Since coordinator has run_village, let's just inspect what Pass 1 shifts are)
    # We can run it using the coordinator object directly!
    print("Running check...")
    # Run pipeline but let's replicate Pass 1 consensus calculations
    all_village_plot_numbers = sorted(village.plots.index.astype(str).tolist())
    
    # Let's check the centroids in UTM
    import geopandas as gpd
    from bhume.score import _utm_for
    utm = _utm_for(village.plots.geometry.iloc[0])
    plots_utm = village.plots.to_crs(utm)
    
    centroids = {str(idx): (geom.centroid.x, geom.centroid.y) for idx, geom in plots_utm.geometry.items()}
        
    p_centroid = centroids["1763"]
    dists = []
    for other_no, other_centroid in centroids.items():
        if other_no == "1763":
            continue
        d = np.hypot(p_centroid[0] - other_centroid[0], p_centroid[1] - other_centroid[1])
        dists.append((d, other_no))
    dists.sort(key=lambda x: x[0])
    
    # Load Pass 1 shifts
    # We can load predictions.geojson from Malatavadi if it's there
    # Wait, in the full benchmark runner, it overwrote predictions.geojson
    # But wait, did we run the pipeline on Malatavadi?
    # Yes, we just ran task-540 which ran run_village on Malatavadi with plot_numbers=["1763"]!
    # Wait, when run_village runs with plot_numbers=["1763"], it still runs Pass 1 on ALL plots!
    # And then it runs Pass 2 only on "1763".
    # But wait! Does it save Pass 1 results anywhere?
    # No, it doesn't save them.
    # Let's run Pass 1 here for the neighbors of 1763!
    # Actually, we can run Pass 1 for all plots in village using the coordinator.
    # Let's do that and print the neighbor shifts.
    pass1_opt_results = {}
    pass1_tasks = []
    dx_global, dy_global = 0.0, 0.0
    
    # Let's identify the 7 nearest neighbors
    neighbors = [x[1] for x in dists[:7]]
    print("Nearest neighbors:", neighbors)
    
    for plot_no in ["1763"] + neighbors:
        pass1_tasks.append((cfg, village_dir, plot_no, dx_global, dy_global, None, True, None))
        
    from bhume.coordinator import _process_plot_worker
    for task in pass1_tasks:
        plot_no, opt_res, _, _, _, err = _process_plot_worker(task)
        if err is None:
            pass1_opt_results[plot_no] = opt_res
            print(f"Plot {plot_no}: Pass 1 shift = ({opt_res.best_candidate.dx:.3f}, {opt_res.best_candidate.dy:.3f}), score = {opt_res.best_score:.4f}")
        else:
            print(f"Plot {plot_no}: Failed: {err}")

if __name__ == "__main__":
    check_shifts()
