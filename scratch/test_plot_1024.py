import os
os.environ["PYTHONPATH"] = "."
from pathlib import Path

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator, _process_plot_worker

def test_single_plot():
    config = Config()
    config.village_name = "Malatavadi"
    config.force_cpu_execution = False
    
    village_dir = Path("data/village_Malatavadi")
    
    coord = PipelineCoordinator(config)
    
    # We need to construct the args tuple for _process_plot_worker
    # The signature in coordinator is typically (plot_no, None, config, None, None, village_dir, ...)
    
    from bhume.loader import DatasetLoader
    loader = DatasetLoader(village_dir)
    plots = loader.load_plots()
    
    # Let's just find plot 1024
    for plot in plots:
        if plot.plot_number == "1024":
            patch = loader.load_image_patch(plot.geometry, margin_m=config.search_radius_m + 15)
            
            from bhume.edge_detector import EdgeDetector
            from bhume.contour_detector import ContourDetector
            
            edge_detector = EdgeDetector(config)
            contour_detector = ContourDetector(config)
            
            edge_res = edge_detector.detect(patch.gray)
            boundary = contour_detector.process(plot.plot_number, plot.geometry)
            
            from bhume.candidate_generator import CandidateGenerator
            from bhume.alignment_scorer import AlignmentScorer
            from bhume.optimizer import LocalOptimizer
            
            generator = CandidateGenerator(config, boundary)
            scorer = AlignmentScorer(config)
            optimizer = LocalOptimizer(config, generator, scorer)
            
            opt_res = optimizer.optimize(
                plot_number=plot.plot_number,
                boundary=boundary,
                edge_level=edge_res,
                transformer=patch.transformer,
                patch_transform=patch.transform,
                patch_gray=patch.gray,
                center_dx=0.0,
                center_dy=0.0,
                recorded_area_m2=plot.recorded_area_m2,
                map_area_m2=plot.geometry.area,
                perimeter_m=plot.geometry.length,
                neighbor_shift=None,
                expected_drift=None,
                coarse_only=False,
            )
            
            print("Best Score:", opt_res.best_score)
            print("Official Score:", getattr(opt_res, "official_score", None))
            print("Top Candidate Shift:", opt_res.best_candidate.dx, opt_res.best_candidate.dy)
            
            import pprint
            pprint.pprint(opt_res.top_k_candidates[0].feature_scores if opt_res.top_k_candidates else {})
            break

if __name__ == "__main__":
    test_single_plot()
