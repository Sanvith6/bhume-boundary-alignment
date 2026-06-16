import sys
import numpy as np
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village
from bhume.coordinator import PipelineCoordinator
from bhume.coordinator import _process_plot_worker

def inspect_1763():
    cfg = Config(workers="auto", cache_enabled=False, debug_visualize=False)
    coordinator = PipelineCoordinator(cfg)
    
    village_dir = Path("data/village_Malatavadi")
    village = load_village(village_dir)
    
    print("Running Pass 1 on all plots to extract shifts...")
    all_village_plot_numbers = sorted(village.plots.index.astype(str).tolist())
    
    pass1_opt_results = {}
    pass1_failed_plots = set()
    pass1_tasks = []
    for plot_no in all_village_plot_numbers:
        pass1_tasks.append((cfg, village_dir, plot_no, 0.0, 0.0, None, True, None))
        
    import multiprocessing
    n_workers = multiprocessing.cpu_count()
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=n_workers) as pool:
        for plot_no, opt_res, _, _, _, err_info in pool.imap_unordered(_process_plot_worker, pass1_tasks):
            if err_info is not None:
                pass1_failed_plots.add(plot_no)
            else:
                pass1_opt_results[plot_no] = opt_res
                
    pass1_shifts = {}
    for plot_no, opt_res in pass1_opt_results.items():
        if plot_no not in pass1_failed_plots and opt_res is not None:
            pass1_shifts[plot_no] = (opt_res.best_candidate.dx, opt_res.best_candidate.dy)
            
    all_dxs = [s[0] for s in pass1_shifts.values()]
    all_dys = [s[1] for s in pass1_shifts.values()]
    village_consensus_dx = float(np.median(all_dxs))
    village_consensus_dy = float(np.median(all_dys))
    print(f"Village Consensus: ({village_consensus_dx:.3f}, {village_consensus_dy:.3f})")
    
    # Let's calculate the degree-based local consensus for 1763
    centroids = {}
    for plot_no in all_village_plot_numbers:
        geom = village.plot(plot_no)
        centroids[plot_no] = (geom.centroid.x, geom.centroid.y)
        
    p_centroid = centroids["1763"]
    dists = []
    for other_no, other_centroid in centroids.items():
        if other_no == "1763":
            continue
        if other_no in pass1_shifts:
            d = np.hypot(p_centroid[0] - other_centroid[0], p_centroid[1] - other_centroid[1])
            dists.append((d, pass1_shifts[other_no], other_no))
            
    dists.sort(key=lambda x: x[0])
    nearest = dists[:7]
    print("Nearest 7 neighbors in degrees:")
    for d, s, name in nearest:
        print(f"  {name}: dist={d:.6f}, shift=({s[0]:.3f}, {s[1]:.3f})")
        
    dx_local = float(np.median([item[1][0] for item in nearest]))
    dy_local = float(np.median([item[1][1] for item in nearest]))
    print(f"Computed Local Consensus: ({dx_local:.3f}, {dy_local:.3f})")

if __name__ == "__main__":
    inspect_1763()
