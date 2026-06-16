import json
with open('debug_output/pipeline/village_Malatavadi_diagnostics.jsonl', 'r') as f:
    for line in f:
        d = json.loads(line)
        if d['plot_number'] == "1763":
            print(d)
