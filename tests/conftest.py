"""Fixtures for the Backend Agent's own tests. Everything runs offline: the MockLLMProvider needs no key and no network."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECTS = ROOT / "projects"
FIXTURES = ["task_manager", "ecommerce_store", "support_ticketing_system"]
FRONTEND_REPO = ROOT.parent / "Frontend-Agent"


@pytest.fixture(scope="session")
def pg_url():
    """A dedicated PostgreSQL (TEST_DATABASE_URL or a throw-away local cluster); tests that need it skip with the reason otherwise."""
    from app.testing import pg_cluster

    try:
        url = pg_cluster.get_database_url()
    except (pg_cluster.DatabaseUnavailable, pg_cluster.UnsafeDatabase) as e:
        pytest.skip(f"no dedicated PostgreSQL available: {e}")
    if not pg_cluster.wait_ready(url, 5):
        pytest.skip("the test database does not accept connections")
    return url


@pytest.fixture()
def project(tmp_path):
    """Copy a fixture project (graphs, database contract, frontend snapshot) so tests can mutate it freely."""

    counter = {"n": 0}

    def make(name: str = "task_manager", *, with_backend: bool = False) -> Path:
        counter["n"] += 1
        dst = tmp_path / str(counter["n"]) / name
        for sub in ("graphs", "validation", "database", "frontend", "input"):
            if (PROJECTS / name / sub).exists():
                shutil.copytree(PROJECTS / name / sub, dst / sub)
        if with_backend and (PROJECTS / name / "backend").exists():
            shutil.copytree(PROJECTS / name / "backend", dst / "backend")
        return dst

    return make


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def mutate_graph(project_dir: Path, graph: str, fn, *, refresh_checksum: bool = True) -> None:
    """Edit one graph file (fn receives the parsed document) and keep the manifest checksum consistent (unless asked not to)."""
    import hashlib

    path = project_dir / "graphs" / f"{graph}.json"
    doc = read_json(path)
    fn(doc)
    write_json(path, doc)
    if refresh_checksum:
        mpath = project_dir / "graphs" / "graph_manifest.json"
        manifest = read_json(mpath)
        manifest["checksums"][graph] = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        write_json(mpath, manifest)
