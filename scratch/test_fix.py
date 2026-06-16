"""Thorough verification: 
1. Test 5 different plots to ensure no crashes
2. Compare GPU vs CPU output for the same plot to verify numerical equivalence
"""
import os, sys, time
os.environ["PYTHONPATH"] = "."

from pathlib import Path
from bhume.config import Config
from bhume.coordinator import _process_plot_worker

config = Config(workers=1)
village_dir = Path("data/village_Malatavadi")

# Test 1: Run 5 different plots
test_plots = ["1", "10", "100", "500", "1024"]
print("=" * 60)
print("TEST 1: Run 5 plots through GPU pipeline")
print("=" * 60)

all_passed = True
for plot_no in test_plots:
    args = (config, str(village_dir), plot_no, 0.0, 0.0)
    t0 = time.time()
    result = _process_plot_worker(args)
    elapsed = time.time() - t0
    
    pno, opt_res, t_pre, t_edges, t_opt, err_info = result
    
    if err_info is not None:
        print(f"  FAIL Plot {pno}: {err_info['exception']}")
        all_passed = False
    else:
        print(f"  OK   Plot {pno}: score={opt_res.best_score:.4f}, shift=({opt_res.best_candidate.dx:.2f}, {opt_res.best_candidate.dy:.2f}), time={elapsed:.0f}s")

print(f"\nTest 1 Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")

# Test 2: Compare GPU vs CPU for same plot
print("\n" + "=" * 60)
print("TEST 2: GPU vs CPU numerical comparison for plot 500")
print("=" * 60)

import torch
import numpy as np
from bhume.coordinator import get_cached_village
from bhume.edge_detector import EdgeDetector, MultiScaleEdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator, CandidateTransformation
from bhume.alignment_scorer import AlignmentScorer
from bhume.preprocessor import Preprocessor

village = get_cached_village(village_dir)
plot_no = "500"
original_geom = village.plot(plot_no)

preprocessor = Preprocessor(config)
patch = preprocessor.extract_patch(village.imagery_path, original_geom, margin_m=config.search_radius_m + config.patch_pad_margin_m)
ms_patch = preprocessor.build_pyramid(patch, config.scale_pyramids)
edge_detector = MultiScaleEdgeDetector(config)
edges = edge_detector.detect_pyramid(ms_patch, method="combined", plot_area_m2=original_geom.area)
edge_level = edges.get_level(1.0)

contour_detector = ContourDetector(config)
boundary = contour_detector.parameterize_boundary(plot_no, original_geom, patch.transformer, image_shape=patch.gray.shape)

# Create a small set of test candidates
cands = [CandidateTransformation(dx=float(dx), dy=float(dy), search_level="test") 
         for dx in range(-5, 6, 1) for dy in range(-5, 6, 1)]

scorer = AlignmentScorer(config)

# GPU run
import bhume.alignment_scorer as asc
assert asc.torch is not None and torch.cuda.is_available(), "GPU not available!"
gpu_results = scorer.score_candidates(cands, boundary, edge_level, patch.transformer, patch.transform,
                                       recorded_area_m2=None, map_area_m2=original_geom.area)
gpu_scores = np.array([r.total_score for r in gpu_results])

# CPU run (temporarily disable torch)
saved_torch = asc.torch
asc.torch = None
cpu_results = scorer.score_candidates(cands, boundary, edge_level, patch.transformer, patch.transform,
                                       recorded_area_m2=None, map_area_m2=original_geom.area)
cpu_scores = np.array([r.total_score for r in cpu_results])
asc.torch = saved_torch  # restore

# Compare
max_diff = np.max(np.abs(gpu_scores - cpu_scores))
mean_diff = np.mean(np.abs(gpu_scores - cpu_scores))
rank_gpu = np.argsort(-gpu_scores)
rank_cpu = np.argsort(-cpu_scores)
same_best = rank_gpu[0] == rank_cpu[0]

print(f"  Candidates tested: {len(cands)}")
print(f"  Max score difference:  {max_diff:.6f}")
print(f"  Mean score difference: {mean_diff:.6f}")
print(f"  GPU best: candidate {rank_gpu[0]} (score={gpu_scores[rank_gpu[0]]:.6f})")
print(f"  CPU best: candidate {rank_cpu[0]} (score={cpu_scores[rank_cpu[0]]:.6f})")
print(f"  Same winner? {same_best}")
print(f"  Scores close enough? {max_diff < 0.001}")

if max_diff < 0.001 and all_passed:
    print("\n*** ALL TESTS PASSED — GPU pipeline verified ***")
else:
    print(f"\n*** WARNING: max_diff={max_diff:.6f}, all_passed={all_passed} ***")
