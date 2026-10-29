import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parent.parent


def hoy_utc():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


# --- helpers -----------------------------------------------------------------

def json_del_dia(api, ip):
    """Contenido del archivo <IP>_<hoy>.json (lista de envios del dia)."""
    path = api.DATA_DIR / f"{ip}_{hoy_utc()}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def filas(api, sql):
    conn = sqlite3.connect(api.DB_PATH)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def assert_problem(resp, status):
    """Todo error debe venir en formato Problem Details (RFC 9457)."""
    assert resp.status_code == status
    assert resp.mimetype == "application/problem+json"
    body = resp.get_json()
    assert set(body) == {"type", "title", "status", "detail"}
    assert body["status"] == status
    return body


# --- health ------------------------------------------------------------------

def test_health_responde_ok(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# --- envio de relevamientos (POST) -----------------------------------------------

def test_envio_valido_se_guarda_en_json_y_en_la_base(api, client, auth, payload):
    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert resp.status_code == 201
    assert resp.get_json()["file"] == f"10.0.0.5_{hoy_utc()}.json"
    # JSON: un envio, con lo enviado mas los datos que agrega el servidor
    [guardado] = json_del_dia(api, "10.0.0.5")
    assert guardado["processes"] == payload["processes"]
    assert "received_at" in guardado and "source_addr" in guardado
    # Base: un servidor, un relevamiento, sus procesos y su sesion
    assert filas(api, "SELECT ip FROM servers") == [("10.0.0.5",)]
    assert filas(api, "SELECT os_name, cpu_logical_cores FROM reports") == [("Linux", 8)]
    assert filas(api, "SELECT pid, name FROM processes ORDER BY pid") == [(1, "systemd"), (42, "nginx")]
    assert filas(api, "SELECT username FROM sessions") == [("rosario",)]


def test_la_fecha_de_recepcion_esta_en_utc_y_coincide_con_el_archivo(api, client, auth, payload):
    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    [guardado] = json_del_dia(api, "10.0.0.5")
    recibido = datetime.datetime.fromisoformat(guardado["received_at"])
    assert recibido.utcoffset() == datetime.timedelta(0)            # termina en +00:00
    assert resp.get_json()["file"] == f"10.0.0.5_{recibido.date().isoformat()}.json"


def test_varios_envios_del_mismo_dia_se_acumulan(api, client, auth, payload):
    for _ in range(3):
        client.post("/api/v1/reports", json=payload, headers=auth)

    assert len(json_del_dia(api, "10.0.0.5")) == 3
    assert filas(api, "SELECT COUNT(*) FROM reports") == [(3,)]
    assert filas(api, "SELECT COUNT(*) FROM servers") == [(1,)]  # el servidor no se duplica


def test_campos_no_pedidos_se_descartan(api, client, auth, payload):
    payload["hostname"] = "srv-01"
    payload["basura"] = "x" * 100
    payload["processes"][0]["cmdline"] = "--password=secreto"

    client.post("/api/v1/reports", json=payload, headers=auth)

    [guardado] = json_del_dia(api, "10.0.0.5")
    assert "hostname" not in guardado and "basura" not in guardado
    assert guardado["processes"][0] == {"pid": 1, "name": "systemd"}


def test_sin_token_devuelve_401(client, payload):
    resp = client.post("/api/v1/reports", json=payload)

    assert_problem(resp, 401)
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_token_incorrecto_devuelve_401(client, payload):
    resp = client.post("/api/v1/reports", json=payload,
                       headers={"Authorization": "Bearer otro-token"})

    assert_problem(resp, 401)


@pytest.mark.parametrize("campo", ["ip", "os_name", "os_version", "cpu", "processes", "users"])
def test_falta_un_campo_obligatorio_devuelve_400(client, auth, payload, campo):
    del payload[campo]

    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert campo in assert_problem(resp, 400)["detail"]


@pytest.mark.parametrize("campo, valor", [
    ("cpu", "intel"),            # debe ser un objeto
    ("processes", "nginx"),      # debe ser una lista
    ("users", ["rosario"]),      # debe ser una lista de objetos
])
def test_tipo_invalido_devuelve_400(client, auth, payload, campo, valor):
    payload[campo] = valor

    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert_problem(resp, 400)


def test_body_que_no_es_json_devuelve_400(client, auth):
    resp = client.post("/api/v1/reports", data="hola", headers=auth)

    assert_problem(resp, 400)


@pytest.mark.parametrize("ip", [
    "../../etc/passwd",          # path traversal
    "10.0.0.1/../../x",
    "999.1.1.1",
    "",
])
def test_ip_invalida_devuelve_400_y_no_escribe_archivos(api, client, auth, payload, ip):
    payload["ip"] = ip

    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert_problem(resp, 400)
    assert list(api.DATA_DIR.glob("*.json")) == []


def test_ipv6_se_guarda_con_un_nombre_de_archivo_valido_en_windows(client, auth, payload):
    payload["ip"] = "fe80::1"

    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert resp.status_code == 201
    assert resp.get_json()["file"].startswith("fe80--1_")  # ':' no es valido en Windows
    consulta = client.get("/api/v1/reports/fe80::1", headers=auth)
    assert consulta.status_code == 200


def test_body_mayor_a_5mb_devuelve_413(client, auth):
    resp = client.post("/api/v1/reports", data=b"x" * (5 * 1024 * 1024 + 1),
                       headers={**auth, "Content-Type": "application/json"})

    assert_problem(resp, 413)


# --- consulta (GET) --------------------------------------------------------------

def test_consulta_devuelve_los_envios_agrupados_por_fecha(client, auth, payload):
    client.post("/api/v1/reports", json=payload, headers=auth)

    resp = client.get("/api/v1/reports/10.0.0.5", headers=auth)

    hoy = hoy_utc()
    assert resp.status_code == 200
    assert list(resp.get_json()["reports_by_date"]) == [hoy]


def test_consulta_filtrando_por_fecha(client, auth, payload):
    client.post("/api/v1/reports", json=payload, headers=auth)
    hoy = hoy_utc()

    resp = client.get(f"/api/v1/reports/10.0.0.5?date={hoy}", headers=auth)

    assert list(resp.get_json()["reports_by_date"]) == [hoy]


def test_consulta_sin_token_devuelve_401(client):
    assert_problem(client.get("/api/v1/reports/10.0.0.5"), 401)


def test_ip_sin_datos_devuelve_404(client, auth):
    assert_problem(client.get("/api/v1/reports/10.9.9.9", headers=auth), 404)


def test_fecha_sin_datos_devuelve_404(client, auth, payload):
    client.post("/api/v1/reports", json=payload, headers=auth)

    resp = client.get("/api/v1/reports/10.0.0.5?date=2001-01-01", headers=auth)

    assert_problem(resp, 404)


@pytest.mark.parametrize("fecha", ["5", "2026-13-01", "ayer"])
def test_fecha_invalida_devuelve_400(client, auth, fecha):
    resp = client.get(f"/api/v1/reports/10.0.0.5?date={fecha}", headers=auth)

    assert_problem(resp, 400)


# --- manejo de errores -------------------------------------------------------------

def test_metodo_no_permitido_devuelve_405(client, auth):
    assert_problem(client.delete("/api/v1/reports", headers=auth), 405)


def test_ruta_inexistente_devuelve_404(client):
    assert_problem(client.get("/no-existe"), 404)


def test_token_no_configurado_devuelve_500_sin_detalles(api, client, auth, monkeypatch):
    monkeypatch.setattr(api, "AGENT_TOKEN", "")

    resp = client.get("/api/v1/reports/10.0.0.5", headers=auth)

    assert "AGENT_TOKEN" not in assert_problem(resp, 500)["detail"]


def test_error_inesperado_devuelve_500_generico(api, client, auth, payload, monkeypatch):
    def falla(*args):
        raise RuntimeError("detalle interno que no debe filtrarse")
    monkeypatch.setattr(api, "insert_report", falla)

    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert "detalle interno" not in assert_problem(resp, 500)["detail"]


# --- guardado atomico (JSON + base) ----------------------------------------------

def test_si_falla_la_base_el_json_queda_como_estaba(api, client, auth, payload, monkeypatch):
    client.post("/api/v1/reports", json=payload, headers=auth)
    antes = json_del_dia(api, "10.0.0.5")

    def falla(*args):
        raise sqlite3.OperationalError("disco lleno")
    monkeypatch.setattr(api, "insert_report", falla)
    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert resp.status_code == 500
    assert json_del_dia(api, "10.0.0.5") == antes          # el JSON no cambio
    assert filas(api, "SELECT COUNT(*) FROM reports") == [(1,)]  # la base tampoco
    assert list(api.DATA_DIR.glob("*.tmp")) == []          # no quedan temporales


def test_si_falla_el_commit_se_restaura_el_json(api, client, auth, payload, monkeypatch):
    client.post("/api/v1/reports", json=payload, headers=auth)
    antes = json_del_dia(api, "10.0.0.5")

    db_real = api.db

    class ConexionQueFallaAlConfirmar:
        """Envuelve la conexion real, pero el COMMIT falla."""
        def __init__(self):
            self.conn = db_real()

        def execute(self, sql, *args):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("commit fallido")
            return self.conn.execute(sql, *args)

        def __getattr__(self, nombre):
            return getattr(self.conn, nombre)

    monkeypatch.setattr(api, "db", ConexionQueFallaAlConfirmar)
    resp = client.post("/api/v1/reports", json=payload, headers=auth)

    assert resp.status_code == 500
    assert json_del_dia(api, "10.0.0.5") == antes
    assert filas(api, "SELECT COUNT(*) FROM reports") == [(1,)]


# --- concurrencia ------------------------------------------------------------------

WORKER = """
import sys
sys.path.insert(0, {api_dir!r})
import app
client = app.app.test_client()
for _ in range({n}):
    resp = client.post("/api/v1/reports", json={payload!r}, headers={auth!r})
    assert resp.status_code == 201, resp.get_json()
"""


def test_envios_simultaneos_desde_varios_procesos_no_se_pierden(api, auth, payload, tmp_path):
    """Simula los workers de gunicorn: 3 procesos escribiendo a la vez."""
    procesos, envios = 3, 20
    script = WORKER.format(api_dir=str(API_DIR), n=envios, payload=payload, auth=auth)
    env = {**__import__("os").environ, "DATA_DIR": str(tmp_path)}

    workers = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(procesos)]
    assert all(w.wait(timeout=120) == 0 for w in workers)

    total = procesos * envios
    assert len(json_del_dia(api, "10.0.0.5")) == total
    assert filas(api, "SELECT COUNT(*) FROM reports") == [(total,)]


# --- base de datos -----------------------------------------------------------------

def test_sql_injection_se_guarda_como_texto(api, client, auth, payload):
    """Gracias a las consultas parametrizadas (?), el nombre malicioso es solo un dato."""
    malicioso = "x'); DROP TABLE servers; --"
    payload["processes"] = [{"pid": 666, "name": malicioso}]

    client.post("/api/v1/reports", json=payload, headers=auth)

    assert filas(api, "SELECT name FROM processes") == [(malicioso,)]
    assert filas(api, "SELECT COUNT(*) FROM servers") == [(1,)]  # la tabla sigue existiendo


def test_las_claves_foraneas_estan_activas(api):
    conn = api.db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO processes(report_id, pid, name) VALUES(999, 1, 'x')")
    finally:
        conn.close()
