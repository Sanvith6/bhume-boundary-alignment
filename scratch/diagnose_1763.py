import sys
import numpy as np
from pathlib import Path

# Add project root to path before importing bhume modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.io import load as load_village
from bhume.geo import open_imagery
from bhume.loader import CoordinateTransformer
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator, CandidateTransformation
from bhume.alignment_scorer import AlignmentScorer

def diagnose_1763():
    cfg = Config(workers=1, cache_enabled=False, debug_visualize=False)
    village = load_village("data/village_Malatavadi")
    plot_no = "1763"
    
    preprocessor = Preprocessor(cfg)
    ms_processor = MultiScaleProcessor(cfg)
    edge_detector = EdgeDetector(cfg)
    contour_detector = ContourDetector(cfg)
    candidate_generator = CandidateGenerator(cfg)
    alignment_scorer = AlignmentScorer(cfg)
    
    with open_imagery(village.imagery_path) as src:
        transformer = CoordinateTransformer(src)
        patch = preprocessor.process_plot(village, plot_no)
        ms_patch = ms_processor.generate_pyramid(patch, village.plot(plot_no))
        edges = edge_detector.detect_pyramid(ms_patch, method="combined")
        edge_level = edges.get_level(1.0)
        boundary = contour_detector.parameterize_boundary(
            plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
        )
        
        row = village.plots.loc[plot_no]
        rec_area = row.get("recorded_area_sqm")
        map_area = row.get("map_area_sqm")
        recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
        map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
        
        # Candidate 1: Optimizer's chosen shift
        opt_dx, opt_dy = -2.525, -0.025
        cand_opt = CandidateTransformation(dx=opt_dx, dy=opt_dy, search_level="sub_meter")
        sc_opt = alignment_scorer.score_candidate(
            cand_opt, boundary, edge_level, transformer, patch.transform,
            recorded_area_m2, map_area_m2
        )
        
        # Candidate 2: True shift
        true_dx, true_dy = 13.919, 0.050
        cand_true = CandidateTransformation(dx=true_dx, dy=true_dy, search_level="sub_meter")
        sc_true = alignment_scorer.score_candidate(
            cand_true, boundary, edge_level, transformer, patch.transform,
            recorded_area_m2, map_area_m2
        )
        
        # Candidate 3: Coarse true shift close matching (in case of submeter differences)
        # Search locally around true shift to find local maximum of the score surface
        print("=== Plot 1763 Score Comparison ===")
        print(f"Optimizer's Choice ({opt_dx:.3f}, {opt_dy:.3f}):")
        print(f"  Total Score: {sc_opt.total_score:.5f}")
        for k, v in sc_opt.feature_scores.items():
            print(f"    {k}: {v:.5f}")
            
        print(f"\nTrue Shift ({true_dx:.3f}, {true_dy:.3f}):")
        print(f"  Total Score: {sc_true.total_score:.5f}")
        for k, v in sc_true.feature_scores.items():
            print(f"    {k}: {v:.5f}")
            
        # Grid search around true shift to find local peak
        best_local_sc = sc_true
        for dx in np.linspace(true_dx - 2.0, true_dx + 2.0, 41):
            for dy in np.linspace(true_dy - 2.0, true_dy + 2.0, 41):
                cand = CandidateTransformation(dx=dx, dy=dy, search_level="sub_meter")
                sc = alignment_scorer.score_candidate(
                    cand, boundary, edge_level, transformer, patch.transform,
                    recorded_area_m2, map_area_m2
                )
                if sc.total_score > best_local_sc.total_score:
                    best_local_sc = sc
                    
        print(f"\nBest Score Found Near True Shift ({best_local_sc.candidate.dx:.3f}, {best_local_sc.candidate.dy:.3f}):")
        print(f"  Total Score: {best_local_sc.total_score:.5f}")
        for k, v in best_local_sc.feature_scores.items():
            print(f"    {k}: {v:.5f}")

if __name__ == "__main__":
    diagnose_1763()
