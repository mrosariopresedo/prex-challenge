import os
import tempfile

# La app lee su configuracion al importarse: se apunta a una carpeta temporal
# para que los tests nunca escriban en el data/ real del proyecto.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="prex-tests-")
os.environ["AGENT_TOKEN"] = "token-de-prueba"

import pytest  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture
def api(tmp_path, monkeypatch):
    """La app con un almacenamiento vacio y propio para cada test."""
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "inventory.db")
    app_module.init_storage()
    return app_module


@pytest.fixture
def client(api):
    """Cliente HTTP de prueba: hace requests a la app sin levantar un servidor."""
    return api.app.test_client()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer token-de-prueba"}


@pytest.fixture
def payload():
    """Un relevamiento valido, como el que envia el agente."""
    return {
        "ip": "10.0.0.5",
        "os_name": "Linux",
        "os_version": "#1 SMP PREEMPT_DYNAMIC",
        "os_release": "6.8.0",
        "cpu": {
            "model": "x86_64",
            "architecture": "x86_64",
            "physical_cores": 4,
            "logical_cores": 8,
            "max_frequency_mhz": 3200.0,
            "usage_percent": 12.5,
        },
        "processes": [{"pid": 1, "name": "systemd"}, {"pid": 42, "name": "nginx"}],
        "users": [{"name": "rosario"}],
    }
