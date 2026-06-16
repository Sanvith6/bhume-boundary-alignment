import sys
import pickle
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village

def diagnose_target_only(vname, target_plots):
    print(f"\n================ {vname} ================", flush=True)
    village_dir = Path("data") / vname
    village = load_village(village_dir)
    cfg = Config()
    
    for plot_no in target_plots:
        plot_no = str(plot_no)
        cache_file = Path(cfg.debug_out_dir) / "cache" / vname / plot_no / "opt_result.pkl"
        if not cache_file.exists():
            print(f"Plot {plot_no}: Cache file not found at {cache_file}", flush=True)
            continue
            
        with open(cache_file, "rb") as f:
            opt_res = pickle.load(f)
            
        # Get official score (dx=0, dy=0)
        official_score = 0.0
        official_candidates = [
            sc for sc in opt_res.optimization_history
            if abs(sc.candidate.dx) < 1e-5 and abs(sc.candidate.dy) < 1e-5
        ]
        if official_candidates:
            official_score = official_candidates[0].total_score
            official_features = official_candidates[0].feature_scores
        else:
            scores = [sc.total_score for sc in opt_res.optimization_history]
            official_score = min(scores) if scores else 0.0
            official_features = {}
            
        best_cand = opt_res.best_candidate
        best_score = opt_res.best_score
        
        # Look up best scored candidate features
        best_scored = None
        min_dist = float('inf')
        for sc in opt_res.optimization_history:
            dist = np.hypot(sc.candidate.dx - best_cand.dx, sc.candidate.dy - best_cand.dy)
            if dist < min_dist:
                min_dist = dist
                best_scored = sc
                
        best_features = best_scored.feature_scores if best_scored else {}
        
        raw_score_improvement = max(best_score - official_score, 0.0)
        improvement_ratio = raw_score_improvement / max(official_score, 0.001)
        shift_magnitude = np.hypot(best_cand.dx, best_cand.dy)
        
        print(f"Plot {plot_no}:", flush=True)
        print(f"  Optimized Shift: ({best_cand.dx:.4f}, {best_cand.dy:.4f}) | Mag: {shift_magnitude:.4f}m", flush=True)
        print(f"  Best Score: {best_score:.6f} | Official Score: {official_score:.6f}", flush=True)
        print(f"  Raw Improvement: {raw_score_improvement:.6f} | Ratio: {improvement_ratio:.4%}", flush=True)
        print(f"  Best Features: {best_features}", flush=True)
        print(f"  Official Features: {official_features}", flush=True)

diagnose_target_only("village_Vadnerbhairav", [622, 1145, 1403, 1476, 1710, 2647])
diagnose_target_only("village_Malatavadi", [1177, 1763, 1966])
