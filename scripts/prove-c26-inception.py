#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from mac.project_inception import run_c26_project_inception_proof


def main() -> int:
    parser = argparse.ArgumentParser(
        description="run the MAC c26 project inception lifecycle proof"
    )
    parser.add_argument("--project-path", default="~/Src/c26")
    args = parser.parse_args()
    proof = run_c26_project_inception_proof(project_path=args.project_path)
    print(json.dumps(proof, indent=2, sort_keys=True))
    return 0 if proof.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
