from bhume.config import Config
from bhume.loader import VillageDataLoader
from bhume.coordinator import VillageCoordinator
import logging

logging.basicConfig(level=logging.DEBUG)
config = Config()
loader = VillageDataLoader("data/village_Malatavadi", config)
coord = VillageCoordinator(config, loader)

plot = loader.get_plot_data("1763")
print("Plot 1763 loaded")

# Let's run just Pass 1 for 1763
boundary, edge_level, patch_transform, patch_gray, map_area_m2 = coord._prepare_plot_data("1763")
from bhume.optimizer import LocalOptimizer
from bhume.candidate_generator import CandidateGenerator
from bhume.alignment_scorer import AlignmentScorer

scorer = AlignmentScorer(config)
generator = CandidateGenerator(config)
optimizer = LocalOptimizer(config, generator, scorer)

res = optimizer.optimize(
    "1763", boundary, edge_level, loader.transformer, patch_transform, patch_gray, 
    map_area_m2=map_area_m2, perimeter_m=boundary.total_length
)
print(f"Pass 1 Best Candidate: dx={res.best_candidate.dx:.4f}, dy={res.best_candidate.dy:.4f}")
