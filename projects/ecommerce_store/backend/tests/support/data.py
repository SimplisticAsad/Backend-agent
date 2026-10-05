"""Static data shared by test modules at collection time (no fixtures, no database)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "backend_app" / "generated" / "spec.json").read_text())
DATA = ROOT / "tests" / "data"


def load_data(name: str):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else None


GRAPH = load_data("graph_contract.json")
FRONTEND = load_data("frontend_contract.json")
CONFLICTS = load_data("known_conflicts.json") or []
ENGINE_KINDS = ("create", "read", "list", "update", "transition", "delete")
OPS = SPEC["operations"]
ENGINE_OPS = [o for o in OPS.values() if o["kind"] in ENGINE_KINDS]
CUSTOM_OPS = [o for o in OPS.values() if o["kind"] in ("custom", "aggregate")]
ENTITY_IDS = sorted(SPEC["entities"])
