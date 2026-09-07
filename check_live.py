"""Opt-in real-provider test: teach, exit process, recall and compare in a new process."""
import json
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from agent import Gemini, Memory, compare, missing_config, teach


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    if missing_config():
        print("Live check unavailable. Missing: " + ", ".join(missing_config()))
        return 2
    if len(sys.argv) == 1:
        experiment = "sc-check-" + uuid.uuid4().hex[:12]
        print(f"Real Gemini calls and persistent Mubit writes in {experiment}", flush=True)
        for phase in ("teach", "compare"):
            subprocess.run([sys.executable, __file__, phase, experiment], check=True)
        print("PASS: new process retrieved persisted teaching lessons and completed comparison.")
        return 0
    phase, experiment = sys.argv[1:]
    model, memory = Gemini(), Memory(experiment)
    events = []
    def emit(kind, **data):
        events.append(dict(type=kind, **data))
        print(f"{phase}: {kind} {data.get('case', '')}", flush=True)
    try:
        if phase == "compare":
            recalled = memory.recall("receiving inspection donor coverage shortage operational judgment")
            if {l["source_case"] for l in recalled} != {"T1", "T2"}:
                raise RuntimeError("Restart recall did not return both teaching incidents")
        (teach if phase == "teach" else compare)(model, memory, uuid.uuid4().hex, emit)
    finally:
        model.close()
        path = Path(__file__).parent / ".demo" / f"{experiment}-{phase}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(events, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
