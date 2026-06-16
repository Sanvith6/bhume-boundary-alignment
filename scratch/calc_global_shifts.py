import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village
from bhume.coordinator import PipelineCoordinator

def calc_shift(vname):
    village_dir = Path("data") / vname
    village = load_village(village_dir)
    plot_numbers = sorted(village.plots.index.astype(str).tolist())
    n_samples = min(20, len(plot_numbers))
    step = max(1, len(plot_numbers) // n_samples)
    sampled_plots = plot_numbers[::step][:n_samples]
    
    import pickle
    cfg = Config()
    
    dx_coarse_list = []
    dy_coarse_list = []
    for plot_no in sampled_plots:
        cache_file = Path(cfg.debug_out_dir) / "cache" / vname / plot_no / "opt_result.pkl"
        if cache_file.exists():
            with open(cache_file, "rb") as f:
                opt_res = pickle.load(f)
                dx_coarse_list.append(opt_res.best_candidate.dx)
                dy_coarse_list.append(opt_res.best_candidate.dy)
    if dx_coarse_list and dy_coarse_list:
        dx_global = float(np.median(dx_coarse_list))
        dy_global = float(np.median(dy_coarse_list))
    else:
        dx_global, dy_global = 0.0, 0.0
        
    print(f"{vname}: dx_global={dx_global:.6f}, dy_global={dy_global:.6f}")
    return dx_global, dy_global

calc_shift("village_Vadnerbhairav")
calc_shift("village_Malatavadi")
