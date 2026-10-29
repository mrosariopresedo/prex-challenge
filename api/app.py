"""
Endpoints:
  POST /api/v1/reports        -> recibe el relevamiento del agente
  GET  /api/v1/reports/<ip>   -> consulta la informacion de una IP
  GET  /health                -> chequeo de estado

Almacenamiento:
  - Archivo JSON plano: data/<IP>_<AAAA-MM-DD>.json (requisito del challenge)
  - SQLite normalizada: data/inventory.db (deseable)
  Ambos se escriben dentro de la misma transaccion (ver store_report).

Errores: formato estandar Problem Details (RFC 9457), mensajes en ingles.
"""
import datetime
import hmac
import ipaddress
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from flask import Flask, abort, jsonify, request
from werkzeug.exceptions import HTTPException

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "inventory.db"
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
# Un relevamiento real pesa ~23 KB (318 procesos). 5 MB deja margen de sobra
# y evita que un envio gigante agote memoria o disco.
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB
CPU_FIELDS = ["model", "architecture", "physical_cores", "logical_cores",
              "max_frequency_mhz", "usage_percent"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL REFERENCES servers(id),
    received_at TEXT NOT NULL, source_addr TEXT,
    os_name TEXT, os_version TEXT, os_release TEXT,
    cpu_model TEXT, cpu_arch TEXT, cpu_physical_cores INTEGER,
    cpu_logical_cores INTEGER, cpu_max_mhz REAL, cpu_usage_percent REAL
);
CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    pid INTEGER, name TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    username TEXT
);
-- servers.ip ya tiene indice por ser UNIQUE
CREATE INDEX IF NOT EXISTS idx_reports_server ON reports(server_id);
CREATE INDEX IF NOT EXISTS idx_processes_report ON processes(report_id);
CREATE INDEX IF NOT EXISTS idx_sessions_report ON sessions(report_id);
"""


def db():
    # isolation_level=None: las transacciones se manejan a mano (BEGIN/COMMIT)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # SQLite no controla las claves foraneas salvo que se active por conexion
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(db()) as conn:
        conn.executescript(SCHEMA)


def require_token():
    """Autenticacion simple por token compartido (Bearer)."""
    if not AGENT_TOKEN:
        # El detalle va al log del servidor; al cliente solo un error generico
        app.logger.error("AGENT_TOKEN is not configured")
        abort(500, "An unexpected error occurred.")
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()
    # compare_digest evita ataques de timing
    if not hmac.compare_digest(token, AGENT_TOKEN):
        abort(401, "Missing or invalid token.")


def valid_ip(value) -> str:
    """Valida la IP. Ademas evita path traversal, ya que la IP
    forma parte del nombre del archivo."""
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        abort(400, f"Invalid IP address: {value!r}.")


def valid_date(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        abort(400, f"Invalid date: {value!r} (expected YYYY-MM-DD).")


def validate_payload(data):
    if not isinstance(data, dict):
        abort(400, "Request body must be a JSON object.")
    required = ["ip", "os_name", "os_version", "cpu", "processes", "users"]
    missing = [k for k in required if k not in data]
    if missing:
        abort(400, f"Missing fields: {', '.join(missing)}.")
    if not isinstance(data["cpu"], dict):
        abort(400, "'cpu' must be an object.")
    for key in ("processes", "users"):
        items = data[key]
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            abort(400, f"'{key}' must be a list of objects.")


def build_report(data: dict, ip: str) -> dict:
    """Se guarda solo lo que pide la consigna, mas lo necesario para el
    funcionamiento (ip, received_at) y la seguridad (source_addr).
    Cualquier otro campo que mande el cliente se descarta."""
    cpu = data["cpu"]
    return {
        "ip": ip,
        # UTC explicito: no depende de la zona horaria del servidor o contenedor
        "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source_addr": request.remote_addr,  # IP desde la que llego la conexion
        "os_name": data["os_name"],
        "os_version": data["os_version"],
        "os_release": data.get("os_release"),
        "cpu": {k: cpu.get(k) for k in CPU_FIELDS},
        "processes": [{"pid": p.get("pid"), "name": p.get("name")} for p in data["processes"]],
        "users": [{"name": u.get("name")} for u in data["users"]],
    }


def file_prefix(ip: str) -> str:
    """Prefijo del archivo para una IP. Los ':' de IPv6 se reemplazan por '-'
    porque Windows no los admite en nombres de archivo."""
    return ip.replace(":", "-")


def write_json_tmp(ip: str, report: dict) -> tuple[Path, Path]:
    """Prepara <IP>_<AAAA-MM-DD>.json en un archivo temporal, sin tocar el real.
    Si hay varios envios en el dia, se acumulan en una lista (no se pierde historial)."""
    day = report["received_at"][:10]  # AAAA-MM-DD en UTC, igual que received_at
    path = DATA_DIR / f"{file_prefix(ip)}_{day}.json"
    reports = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    reports.append(report)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(reports, f, indent=2, ensure_ascii=False)
    return path, Path(tmp)


def insert_report(conn, ip: str, r: dict):
    cpu = r["cpu"]
    conn.execute("INSERT INTO servers(ip) VALUES(?) ON CONFLICT(ip) DO NOTHING", (ip,))
    server_id = conn.execute("SELECT id FROM servers WHERE ip=?", (ip,)).fetchone()["id"]
    cur = conn.execute(
        """INSERT INTO reports(server_id, received_at, source_addr, os_name, os_version,
           os_release, cpu_model, cpu_arch, cpu_physical_cores, cpu_logical_cores,
           cpu_max_mhz, cpu_usage_percent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (server_id, r["received_at"], r["source_addr"], r["os_name"], r["os_version"],
         r["os_release"], cpu["model"], cpu["architecture"], cpu["physical_cores"],
         cpu["logical_cores"], cpu["max_frequency_mhz"], cpu["usage_percent"]),
    )
    rid = cur.lastrowid
    conn.executemany(
        "INSERT INTO processes(report_id, pid, name) VALUES(?,?,?)",
        [(rid, p["pid"], p["name"]) for p in r["processes"]],
    )
    conn.executemany(
        "INSERT INTO sessions(report_id, username) VALUES(?,?)",
        [(rid, u["name"]) for u in r["users"]],
    )


def store_report(ip: str, report: dict) -> Path:
    """Guarda en JSON y en SQLite como una sola operacion: o quedan ambos, o ninguno.

    1. BEGIN IMMEDIATE toma el lock de escritura de SQLite. Ademas serializa a los
       workers de gunicorn, asi dos envios simultaneos no se pisan el JSON del dia.
    2. Se arma el JSON nuevo en un temporal y se insertan las filas (sin confirmar).
    3. os.replace reemplaza el archivo de forma atomica (todo o nada).
    4. COMMIT. Si algo falla antes, ROLLBACK y se restaura el JSON anterior.
    """
    with closing(db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        path = tmp = previous = None
        replaced = False
        try:
            path, tmp = write_json_tmp(ip, report)
            previous = path.read_bytes() if path.exists() else None
            insert_report(conn, ip, report)
            os.replace(tmp, path)
            replaced = True
            conn.execute("COMMIT")
            return path
        except Exception:
            conn.execute("ROLLBACK")
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            if replaced:
                # compensacion: se vuelve al JSON que habia antes de este envio
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
            raise


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/reports")
def receive_report():
    require_token()
    data = request.get_json(silent=True)
    validate_payload(data)
    ip = valid_ip(data["ip"])
    path = store_report(ip, build_report(data, ip))
    return jsonify({"message": "Report stored.", "file": path.name}), 201


@app.get("/api/v1/reports/<ip>")
def get_reports(ip):
    require_token()
    ip = valid_ip(ip)
    date = request.args.get("date")  # opcional: AAAA-MM-DD
    if date:
        date = valid_date(date)
        files = [DATA_DIR / f"{file_prefix(ip)}_{date}.json"]
        files = [f for f in files if f.exists()]
    else:
        files = sorted(DATA_DIR.glob(f"{file_prefix(ip)}_*.json"))
    if not files:
        detail = f" on {date}" if date else ""
        abort(404, f"No data for {ip}{detail}.")
    result = {}
    for f in files:
        result[f.stem.split("_", 1)[1]] = json.loads(f.read_text(encoding="utf-8"))
    return jsonify({"ip": ip, "reports_by_date": result})


def problem(status: int, title: str, detail: str):
    """Respuesta de error con formato Problem Details (RFC 9457)."""
    resp = jsonify({"type": "about:blank", "title": title, "status": status, "detail": detail})
    resp.status_code = status
    resp.mimetype = "application/problem+json"
    return resp


@app.errorhandler(HTTPException)
def handle_http_error(e):
    """Cualquier error HTTP (400, 401, 404, 405, 413...) con el mismo formato."""
    resp = problem(e.code, e.name, e.description)
    if e.code == 401:
        # RFC 6750: un 401 debe indicar que esquema de autenticacion se espera
        resp.headers["WWW-Authenticate"] = "Bearer"
    return resp


@app.errorhandler(Exception)
def handle_unexpected(e):
    """Errores no previstos: el detalle va al log, al cliente un mensaje generico."""
    app.logger.exception("Unhandled error")
    return problem(500, "Internal Server Error", "An unexpected error occurred.")


init_storage()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
