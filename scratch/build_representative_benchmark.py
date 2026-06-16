"""Build a representative benchmark set for the optimization protocol.

Includes:
- All public example truth plots
- First-order neighbors (plots sharing a boundary with truth plots)
- Second-order neighbors (plots sharing a boundary with first-order neighbors)
- Random spatial samples across each village (10% of remaining plots)
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.io import load as load_village
from bhume.loader import build_neighbor_graph


def build_benchmark():
    random.seed(42)  # Deterministic sampling
    benchmark = {}

    for village_name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / village_name
        village = load_village(village_dir)
        all_plot_numbers = sorted(village.plots.index.astype(str).tolist())

        # Build neighbor graph
        graph = build_neighbor_graph(village.plots)

        # 1. Public example truth plots
        truth_plots = set()
        if village.example_truths is not None:
            truth_plots = set(village.example_truths.index.astype(str).tolist())

        # 2. First-order neighbors
        first_order = set()
        for pn in truth_plots:
            neighbors = graph.get(pn, {})
            first_order.update(neighbors.keys())
        first_order -= truth_plots  # Remove truths themselves

        # 3. Second-order neighbors
        second_order = set()
        for pn in first_order | truth_plots:
            neighbors = graph.get(pn, {})
            second_order.update(neighbors.keys())
        second_order -= truth_plots
        second_order -= first_order

        # 4. Random spatial samples (10% of remaining plots)
        included = truth_plots | first_order | second_order
        remaining = [pn for pn in all_plot_numbers if pn not in included]
        sample_size = max(10, len(remaining) // 10)
        random_sample = set(random.sample(remaining, min(sample_size, len(remaining))))

        # Combine and sort
        final_set = sorted(truth_plots | first_order | second_order | random_sample)

        benchmark[village_name] = final_set
        print(f"{village_name}:")
        print(f"  Total plots in village: {len(all_plot_numbers)}")
        print(f"  Truth plots:           {len(truth_plots)}")
        print(f"  First-order neighbors: {len(first_order)}")
        print(f"  Second-order neighbors:{len(second_order)}")
        print(f"  Random spatial sample: {len(random_sample)}")
        print(f"  Final benchmark size:  {len(final_set)}")

    # Save
    out_path = Path("scratch") / "benchmark_plots.json"
    with open(out_path, "w") as f:
        json.dump(benchmark, f, indent=4)
    print(f"\nSaved benchmark to {out_path}")
    return benchmark


if __name__ == "__main__":
    build_benchmark()
