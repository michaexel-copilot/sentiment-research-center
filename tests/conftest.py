import os
import tempfile
from pathlib import Path

import pytest

# Isolate the test DB/cache from real data before any src_center import resolves paths.
_tmp = Path(tempfile.mkdtemp(prefix="src_test_"))


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch):
    from src_center import config
    from src_center.storage import db

    settings = dict(config.settings())
    settings["paths"] = {"db": str(_tmp / "test.duckdb"), "cache": str(_tmp / "cache"), "reports": str(_tmp / "reports")}
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "PROJECT_ROOT", _tmp)
    db.close()
    (_tmp / "test.duckdb").unlink(missing_ok=True)
    os.environ["SRC_LLM_PROVIDER"] = "none"
    yield
    db.close()
