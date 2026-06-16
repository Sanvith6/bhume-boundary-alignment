import sys
import pickle
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village
from bhume.coordinator import PipelineCoordinator, make_placeholder_regularized_result
from bhume.loader import CoordinateTransformer
from bhume.geo import open_imagery

def diagnose_no_reg(vname, target_plots):
    print(f"\n================ {vname} ================", flush=True)
    village_dir = Path("data") / vname
    village = load_village(village_dir)
    cfg = Config()
    coordinator = PipelineCoordinator(cfg)
    
    with open_imagery(village.imagery_path) as src:
        transformer = CoordinateTransformer(src)
        for plot_no in target_plots:
            plot_no = str(plot_no)
            cache_file = Path(cfg.debug_out_dir) / "cache" / vname / plot_no / "opt_result.pkl"
            if not cache_file.exists():
                print(f"Plot {plot_no}: Cache file not found", flush=True)
                continue
                
            with open(cache_file, "rb") as f:
                opt_res = pickle.load(f)
                
            # Create a dummy regularized result using the optimized candidate
            reg_res = make_placeholder_regularized_result(opt_res.best_candidate)
            reg_res.applied_shift = (opt_res.best_candidate.dx, opt_res.best_candidate.dy)
            
            try:
                conf_res = coordinator.confidence_estimator.estimate(opt_res, reg_res)
                dec_res = coordinator.decision_engine.decide(
                    opt_res, reg_res, conf_res, village.plot(plot_no), transformer
                )
                
                print(f"Plot {plot_no}:", flush=True)
                print(f"  Status: {dec_res.status}", flush=True)
                print(f"  Vetoes: {dec_res.decision_reason}", flush=True)
                print(f"  Confidence: {dec_res.confidence:.4f} (Raw: {conf_res.raw_confidence:.4f})", flush=True)
                print(f"  Applied Shift: ({dec_res.applied_shift[0]:.2f}, {dec_res.applied_shift[1]:.2f})", flush=True)
                print(f"  Correctability Score: {dec_res.correctability_score:.4f}", flush=True)
            except Exception as e:
                print(f"Error for plot {plot_no}: {e}", flush=True)

diagnose_no_reg("village_Vadnerbhairav", [622, 1145, 1403, 1476, 1710, 2647])
diagnose_no_reg("village_Malatavadi", [1177, 1763, 1966])
