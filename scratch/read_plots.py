import json

def check_plots(path, ids):
    print(f"=== {path} ===")
    with open(path) as f:
        data = json.load(f)
    for feat in data['features']:
        props = feat['properties']
        pnum_str = props.get('plot_number')
        if pnum_str is not None:
            try:
                pnum = int(pnum_str)
            except ValueError:
                continue
            if pnum in ids:
                print(f"ID: {pnum}")
                print(f"  status: {props.get('status')}")
                print(f"  confidence: {props.get('confidence')}")
                print(f"  method_note: {props.get('method_note')}")

check_plots('data/village_Vadnerbhairav/predictions.geojson', [622, 1145, 1403, 1476, 1710, 2647])
check_plots('data/village_Malatavadi/predictions.geojson', [1177, 1763, 1966])
