# Tests de la API

La suite está en `test_app.py` (41 casos, incluidos los parametrizados). Cada test arranca con un almacenamiento vacío y propio, en una carpeta temporal, así que nunca toca el `data/` real.

```bash
pip install -r api/requirements-dev.txt
cd api && pytest -v
```

## Envío de relevamientos (`POST /api/v1/reports`)

| Test | Qué verifica |
|---|---|
| `test_envio_valido_se_guarda_en_json_y_en_la_base` | Un envío correcto responde 201 y queda guardado en el JSON del día y en las cuatro tablas |
| `test_la_fecha_de_recepcion_esta_en_utc_y_coincide_con_el_archivo` | `received_at` está en UTC (`+00:00`) y su fecha coincide con la del nombre del archivo |
| `test_varios_envios_del_mismo_dia_se_acumulan` | Varios envíos del mismo día se agregan al JSON sin pisarse, y el servidor no se duplica en la base |
| `test_campos_no_pedidos_se_descartan` | Los campos que no pide la consigna (por ejemplo `hostname` o la línea de comandos de un proceso) no se guardan |
| `test_sin_token_devuelve_401` | Sin token se rechaza con 401 y encabezado `WWW-Authenticate: Bearer` |
| `test_token_incorrecto_devuelve_401` | Un token incorrecto se rechaza con 401 |
| `test_falta_un_campo_obligatorio_devuelve_400` | Si falta cualquiera de los 6 campos obligatorios, responde 400 indicando cuál |
| `test_tipo_invalido_devuelve_400` | `cpu` que no es objeto, `processes` que no es lista o `users` con elementos que no son objetos dan 400 |
| `test_body_que_no_es_json_devuelve_400` | Un body que no es JSON da 400 |
| `test_ip_invalida_devuelve_400_y_no_escribe_archivos` | IPs inválidas o con path traversal (`../../etc/passwd`) dan 400 y no se escribe ningún archivo |
| `test_ipv6_se_guarda_con_un_nombre_de_archivo_valido_en_windows` | Una IPv6 se guarda con `-` en lugar de `:` en el nombre del archivo y se puede consultar |
| `test_body_mayor_a_5mb_devuelve_413` | Un body de más de 5 MB se rechaza con 413 |

## Consulta (`GET /api/v1/reports/<ip>`)

| Test | Qué verifica |
|---|---|
| `test_consulta_devuelve_los_envios_agrupados_por_fecha` | La consulta devuelve los envíos de la IP agrupados por fecha |
| `test_consulta_lee_de_la_base_y_coincide_con_el_json` | La consulta responde desde la base (funciona aun borrando los JSON) y devuelve exactamente lo mismo que quedó guardado en el JSON |
| `test_consulta_filtrando_por_fecha` | El filtro `?date=` devuelve solo esa fecha |
| `test_consulta_sin_token_devuelve_401` | La consulta también exige token |
| `test_ip_sin_datos_devuelve_404` | Una IP sin relevamientos da 404 |
| `test_fecha_sin_datos_devuelve_404` | Una fecha sin relevamientos da 404 |
| `test_fecha_invalida_devuelve_400` | Fechas mal formadas o inexistentes (`5`, `2026-13-01`, `ayer`) dan 400 |

## Manejo de errores

Todos los tests de error verifican además que la respuesta cumpla el formato RFC 9457 (`application/problem+json` con `type`, `title`, `status` y `detail`).

| Test | Qué verifica |
|---|---|
| `test_health_responde_ok` | `/health` responde 200 |
| `test_metodo_no_permitido_devuelve_405` | Un método no soportado da 405 |
| `test_ruta_inexistente_devuelve_404` | Una ruta desconocida da 404 |
| `test_token_no_configurado_devuelve_500_sin_detalles` | Sin `AGENT_TOKEN` configurado responde 500 sin revelar el motivo al cliente |
| `test_error_inesperado_devuelve_500_generico` | Un error no previsto responde 500 genérico, sin filtrar el detalle interno |

## Guardado atómico, concurrencia y base de datos

| Test | Qué verifica |
|---|---|
| `test_si_falla_la_base_el_json_queda_como_estaba` | Si la base falla a mitad del guardado, el JSON y la base quedan como antes y no quedan archivos temporales |
| `test_si_falla_el_commit_se_restaura_el_json` | Si falla el `COMMIT`, se restaura el JSON anterior |
| `test_envios_simultaneos_desde_varios_procesos_no_se_pierden` | 3 procesos enviando 20 relevamientos a la vez (como los workers de gunicorn): los 60 quedan en el JSON y en la base |
| `test_sql_injection_se_guarda_como_texto` | Un nombre de proceso con SQL malicioso se guarda como texto y las tablas siguen intactas |
| `test_las_claves_foraneas_estan_activas` | La base rechaza un proceso que apunta a un relevamiento inexistente |

## CI (`.github/workflows/ci.yml`)

| Job | Qué hace |
|---|---|
| Tests (ubuntu-latest, windows-latest) | Corre la suite y el agente en modo `--dry-run` en ambos sistemas |
| Docker de punta a punta | Solo si los tests pasaron: levanta la API con `docker compose`, ejecuta el agente real, verifica que se guardaron el JSON y la base, y que un token incorrecto da 401 |
