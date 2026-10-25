#!/usr/bin/env python3
"""Root-review-gated terminal bookkeeping; no native mutation or task dispatch."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fleet.terminal_review import run_review

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="append the exact reviewed disposition")
    args = parser.parse_args()
    print(json.dumps(run_review(apply=args.apply)), flush=True)
