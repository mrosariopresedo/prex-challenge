import argparse
import json
import os
import platform
import socket
import sys

import psutil
import requests


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_cpu_info() -> dict:
    freq = psutil.cpu_freq()
    return {
        "model": platform.processor() or platform.machine(),
        "architecture": platform.machine(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "max_frequency_mhz": round(freq.max, 2) if freq else None,
        "usage_percent": psutil.cpu_percent(interval=1),
    }


def get_processes() -> list:
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # el proceso termino o no tenemos permisos: se omite
    return procs


def get_users() -> list:
    return [{"name": u.name} for u in psutil.users()]


def collect() -> dict:
    return {
        "ip": get_local_ip(),
        "os_name": platform.system(),
        "os_version": platform.version(),
        "os_release": platform.release(),
        "cpu": get_cpu_info(),
        "processes": get_processes(),
        "users": get_users(),
    }


def main():
    parser = argparse.ArgumentParser(description="Agente de relevamiento")
    parser.add_argument("--api", default=os.getenv("API_URL", "http://localhost:5000"))
    parser.add_argument("--token", default=os.getenv("AGENT_TOKEN"))
    parser.add_argument("--dry-run", action="store_true", help="Solo imprime, no envia")
    args = parser.parse_args()

    data = collect()
    if args.dry_run:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if not args.token:
        sys.exit("Missing token: use --token or the AGENT_TOKEN environment variable")

    resp = requests.post(
        f"{args.api.rstrip('/')}/api/v1/reports",
        json=data,
        headers={"Authorization": f"Bearer {args.token}"},
        timeout=15,
    )
    print(f"[{resp.status_code}] {resp.text}")
    resp.raise_for_status()


if __name__ == "__main__":
    main()
