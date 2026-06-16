from pathlib import Path
import time
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator

def test_gpu_plot():
    print("Testing GPU PyTorch Bridge on Plot 1780...")
    village_dir = Path("data/village_Malatavadi")
    
    cfg = Config(workers=1)
    coordinator = PipelineCoordinator(cfg)
    
    start_time = time.time()
    result = coordinator.run_village(village_dir, plot_numbers=["1780"])
    end_time = time.time()
    
    print(f"\nGPU Test Results:")
    print(f"Time taken: {end_time - start_time:.2f} seconds")
    print(f"Corrected: {result.corrected}")
    print(f"Failed: {result.failed}")

if __name__ == "__main__":
    test_gpu_plot()
