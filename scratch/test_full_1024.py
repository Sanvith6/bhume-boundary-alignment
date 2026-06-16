import os
os.environ["PYTHONPATH"] = "."
from bhume.config import Config
from bhume.coordinator import PipelineCoordinator

config = Config()
config.village_name = "Malatavadi"
config.force_cpu_execution = False

coord = PipelineCoordinator(config)
coord._setup_run()

for p in coord.plots:
    if p.plot_number == "1024":
        res = coord.process_plot(p, pass_num=1)
        res2 = coord.process_plot(p, pass_num=2)
        print("Pass 1 Shift:", res.applied_shift)
        print("Pass 2 Shift:", res2.applied_shift)
        print("Final Status:", res2.status)
        print("Vetoes:", res2.decision_reason)
        print("Decision Trace:")
        import pprint
        pprint.pprint(res2.decision_trace)
        break
