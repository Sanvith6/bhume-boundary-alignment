from pathlib import Path
from bhume.io import load as load_village
from bhume.score import score as score_predictions


def evaluate_all():
    print("=== Pipeline Evaluation Scorecards ===")
    
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / name
        pred_path = village_dir / "predictions.geojson"
        
        if not pred_path.exists():
            print(f"\n{name}: No predictions found at {pred_path}.")
            continue
            
        try:
            village = load_village(village_dir)
            scorecard = score_predictions(pred_path, village)
            print()
            print(scorecard)
        except Exception as e:
            print(f"\n{name}: Evaluation failed: {e}")


if __name__ == "__main__":
    evaluate_all()
