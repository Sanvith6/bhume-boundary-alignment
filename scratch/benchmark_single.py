import sys
import time
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator
from bhume.alignment_scorer import AlignmentScorer
from bhume.optimizer import LocalOptimizer
from bhume.geo import open_imagery
from bhume.loader import CoordinateTransformer

def main():
    village_dir = Path("data") / "village_Malatavadi"
    village = load_village(village_dir)
    plot_no = "100"
    
    print(f"Benchmarking plot {plot_no} across adaptive_step_multiplier values...")
    
    for multiplier in [0.5, 1.0, 1.5, 2.0]:
        print(f"\n--- adaptive_step_multiplier = {multiplier} ---")
        cfg = Config(
            workers=1,
            cache_enabled=False,
            debug_visualize=False,
            adaptive_step_multiplier=multiplier
        )
        
        preprocessor = Preprocessor(cfg)
        ms_processor = MultiScaleProcessor(cfg)
        edge_detector = EdgeDetector(cfg)
        contour_detector = ContourDetector(cfg)
        candidate_generator = CandidateGenerator(cfg)
        alignment_scorer = AlignmentScorer(cfg)
        optimizer = LocalOptimizer(cfg, candidate_generator, alignment_scorer)
        
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
            
            t0 = time.perf_counter()
            opt_res = optimizer.optimize(
                plot_number=plot_no,
                boundary=boundary,
                edge_level=edge_level,
                transformer=transformer,
                patch_transform=patch.transform,
                patch_gray=patch.gray,
                center_dx=0.0,
                center_dy=0.0,
                recorded_area_m2=recorded_area_m2,
                map_area_m2=map_area_m2,
            )
            t_opt = time.perf_counter() - t0
            print(f"Optimization: {t_opt*1000:.1f}ms")
            print(f"Total candidate count evaluated: {len(opt_res.optimization_history)}")
            print(f"Best score: {opt_res.best_score:.4f} at dx={opt_res.best_candidate.dx:.3f}, dy={opt_res.best_candidate.dy:.3f}")

if __name__ == "__main__":
    main()
