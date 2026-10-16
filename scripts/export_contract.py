"""Regenerate docs/contract/v1/schemas.json from app.contract.v1.

python -m scripts.export_contract
"""

import json
from pathlib import Path

from app.contract.v1 import build_contract

TARGET = Path(__file__).resolve().parent.parent / "docs" / "contract" / "v1" / "schemas.json"


def render() -> str:
    return json.dumps(build_contract(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    TARGET.write_text(render())
    print(f"wrote {TARGET}")
