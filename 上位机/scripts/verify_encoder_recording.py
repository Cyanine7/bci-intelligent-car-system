"""Offline full-total calibration audit; does not import Qt or open hardware."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from car_host.encoder_verification import verify_encoder_recording

result = verify_encoder_recording(sys.argv[1])
path = Path(sys.argv[1])/"encoder_verification.json"
path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({k:v for k,v in result.items() if k != "calibrations"},ensure_ascii=False))
raise SystemExit(0 if result["verified"] else 1)
