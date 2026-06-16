"""Run full pipeline on ALL plots (not just the 9 sample ones) to check generalization."""
import json
from pathlib import Path
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator

def run_full(village_name):
    village_dir = Path(f"data/{village_name}")
    print(f"\n{'='*60}")
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    
    cfg = Config(workers=1)
    coordinator = PipelineCoordinator(cfg)
    
    # Run on ALL plots (no plot_numbers filter)
    result = coordinator.run_village(village_dir)
    
    print(f"\n--- {village_name} Full Results ---")
    print(f"Total plots:      {result.total_plots}")
    print(f"Processed:        {result.processed_plots}")
    print(f"Corrected:        {result.corrected}")
    print(f"Flagged:          {result.flagged}")
    print(f"Failed:           {result.failed}")
    print(f"Avg Confidence:   {result.average_confidence:.4f}")
    print(f"Avg Shift (m):    {result.average_shift:.4f}")
    print(f"Avg Improvement:  {result.average_improvement:.4f}")
    print(f"Runtime:          {result.runtime_seconds:.1f}s")
    
    # Read the diagnostics for distribution analysis
    diag_path = Path("debug_output/pipeline") / f"{village_name}_diagnostics.jsonl"
    if diag_path.exists():
        records = []
        with open(diag_path) as f:
            for line in f:
                records.append(json.loads(line))
        
        corrected = [r for r in records if r["decision"] == "corrected"]
        flagged = [r for r in records if r["decision"] == "flagged"]
        
        if corrected:
            confs = [r["confidence"] for r in corrected]
            raw_confs = [r["raw_confidence"] for r in corrected]
            improvements = [r["score_improvement"] for r in corrected]
            translations = [r["translation"] for r in corrected]
            
            import numpy as np
            print(f"\n--- Corrected Plots Distribution ---")
            print(f"  Count:           {len(corrected)}")
            print(f"  Confidence:      min={min(confs):.3f}, median={np.median(confs):.3f}, max={max(confs):.3f}, std={np.std(confs):.3f}")
            print(f"  Raw Confidence:  min={min(raw_confs):.3f}, median={np.median(raw_confs):.3f}, max={max(raw_confs):.3f}, std={np.std(raw_confs):.3f}")
            print(f"  Improvement:     min={min(improvements):.4f}, median={np.median(improvements):.4f}, max={max(improvements):.4f}")
            print(f"  Translation (m): min={min(translations):.2f}, median={np.median(translations):.2f}, max={max(translations):.2f}")
        
        if flagged:
            flag_reasons = {}
            for r in flagged:
                for reason in r["reason_for_flag"]:
                    flag_reasons[reason] = flag_reasons.get(reason, 0) + 1
            print(f"\n--- Flagged Plots Distribution ---")
            print(f"  Count: {len(flagged)}")
            print(f"  Reasons: {json.dumps(flag_reasons, indent=4)}")
    
    return result

if __name__ == "__main__":
    run_full("village_Malatavadi")
    run_full("village_Vadnerbhairav")
