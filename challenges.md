# Engineering Challenges & Solutions

Throughout the development of the BhuMe Boundary Alignment pipeline, we encountered several severe technical bottlenecks—primarily revolving around memory management, hardware limitations, and mathematical "hallucinations." 

Below is a complete breakdown of the major challenges faced and the exact engineering architecture used to solve them.

---

## 1. Out-Of-Memory (OOM) Array Allocations
**The Challenge:** 
The core of the alignment math relies on `scipy.ndimage.map_coordinates` to test thousands of shifted plot boundaries against the satellite image. For tiny residential plots (e.g., 18-meter search radii), this is trivial. However, for massive agricultural fields (like Plot 233, which required a 329-meter search radius), creating a 1.0-meter resolution search grid required allocating multi-gigabyte `float64` coordinate matrices. When multiple CPU workers attempted this simultaneously, it caused immediate memory spikes that crashed the Python interpreter with `Unable to allocate X MiB`.

**The Solution:**
Instead of blindly calculating 1.0-meter grids across massive distances, we engineered a **Multi-Scale Pyramidal Search**:
- **Coarse Phase:** Scans the massive 300m area at a highly compressed 0.25 scale (using tiny arrays).
- **Fine Phase:** Once the general peak is found, the 1.0m resolution grid is restricted strictly to a micro-area around that peak, dropping the RAM requirement for large farms from gigabytes down to megabytes.

## 2. CPU Starvation & Thread Contention
**The Challenge:** 
Initially, the pipeline utilized unbounded multiprocessing (spawning a worker for every available logical core). Because the matrix convolution math was so incredibly dense, the CPU became entirely starved trying to context-switch between dozens of massive array calculations. This completely locked up the operating system.

**The Solution:**
We deliberately throttled the `multiprocessing.Pool` down to exactly **6 strict workers**. By intentionally leaving hardware headroom and trading theoretical speed for absolute system stability, the pipeline was able to run safely in the background overnight without disrupting the OS.

## 3. Long-Running Memory Fragmentation
**The Challenge:** 
Even after fixing the immediate OOM spikes and CPU starvation, a "slow-bleed" crash emerged. After about 9 hours of continuous processing, the pipeline would inexplicably crash. The diagnosis revealed that Python's worker threads were suffering from memory fragmentation—they were holding onto the massive NumPy arrays from previous plots and failing to flush the garbage-collected RAM back to the OS.

**The Solution:**
We injected `maxtasksperchild=10` into the `coordinator.py` execution pool. This created an architectural "self-destruct" mechanism. Every time a worker thread finished processing 10 plots, it was forced to terminate and reboot, guaranteeing that all fragmented memory was physically flushed from RAM. This fix stabilized the pipeline instantly, allowing it to process all 5,000 plots flawlessly.

## 4. Visual Hallucinations & Topological Drift
**The Challenge:** 
An early version of the algorithm relied entirely on Canny edge-detection scores. While this worked for isolated farms, it failed terribly in dense areas. A tiny residential plot might "see" the incredibly sharp, high-contrast edge of a massive neighboring barn and mathematically snap to it, completely ignoring its own actual (but blurrier) borders. 

**The Solution:**
We built a **14-Signal Log-Odds Evidence Accumulator**. We stopped relying solely on visual edges, and built an absolute "Judge." Before a plot is allowed to shift, the Accumulator verifies:
1. **Neighborhood Overlap:** Does this shift crash into an adjacent farm? (Immediate veto).
2. **Area Conservation:** Did the shift accidentally shrink the farm by more than 5%? (Veto).
3. **Rotational Extremes:** Did the farm have to twist 15 degrees just to find an edge? (Veto).

By forcing the algorithm to prove its shifts topologically—not just visually—we successfully eliminated hallucinatory drift and forced the system to return a `Flagged` status rather than making a bad guess.
