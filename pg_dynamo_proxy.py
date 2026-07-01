#!/usr/bin/env python3
"""
DynamoDB PostgreSQL Proxy
=========================
Implementa o protocolo wire do PostgreSQL e traduz SELECTs para DynamoDB Scan.
O Grafana (ou qualquer cliente PostgreSQL) conecta nesse serviço como se fosse
um banco de dados PostgreSQL real — sem drivers extras, sem plugins.

Tabelas disponíveis:
  - audit_log  → cs-audit-log  (DynamoDB)
  - app_logs   → cs-app-logs   (DynamoDB)

Uso no Grafana:
  - Datasource: PostgreSQL
  - Host: cs-dynamo-pg-proxy:5432
  - Database: dynamo
  - User: grafana / Password: grafana
  - SSL: disable
"""

import asyncio
import struct
import logging
import re
import os
import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Configuração via env ────────────────────────────────────────────────────
PG_HOST         = os.getenv("PG_HOST",         "0.0.0.0")
PG_PORT         = int(os.getenv("PG_PORT",     "5450"))
DYNAMO_ENDPOINT = os.getenv("DYNAMO_ENDPOINT", "http://cs-dynamo-db:8000")
DYNAMO_REGION   = os.getenv("DYNAMO_REGION",   "us-east-1")
AWS_ACCESS_KEY  = os.getenv("AWS_ACCESS_KEY_ID",     "AKIAIOSFODNN7EXAMPLE")
AWS_SECRET_KEY  = os.getenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
TABLE_AUDIT     = os.getenv("DYNAMO_TABLE_AUDIT",    "cs-audit-log")
TABLE_APPLOGS   = os.getenv("DYNAMO_TABLE_APPLOGS",  "cs-app-logs")

# ── PostgreSQL type OIDs ────────────────────────────────────────────────────
OID_TEXT = 25
OID_INT4 = 23

# ── Schema das tabelas ──────────────────────────────────────────────────────
# Cada coluna: (nome_sql, campo_dynamo, oid, tipo_dynamo)
# tipo_dynamo: "S" = string  |  "N" = number

AUDIT_LOG_COLS = [
    ("timestamp",   "SK",          OID_TEXT, "S"),
    ("service",     "ServiceName", OID_TEXT, "S"),
    ("operation",   "Operation",   OID_TEXT, "S"),
    ("changed_by",  "ChangedBy",   OID_TEXT, "S"),
    ("resource_id", "ResourceId",  OID_TEXT, "S"),
    ("ip_address",  "IpAddress",   OID_TEXT, "S"),
    ("pk",          "PK",          OID_TEXT, "S"),
    ("payload",     "Payload",     OID_TEXT, "S"),
]

APP_LOGS_COLS = [
    ("timestamp",      "Timestamp",     OID_TEXT, "S"),
    ("level",          "LogLevel",      OID_TEXT, "S"),
    ("caller",         "Caller",        OID_TEXT, "S"),
    ("message",        "Message",       OID_TEXT, "S"),
    ("correlation_id", "CorrelationId", OID_TEXT, "S"),
    ("data",           "Data",          OID_TEXT, "S"),
    ("type",           "Type",          OID_INT4, "N"),
]

TABLES: dict[str, dict] = {
    "audit_log": {
        "dynamo_table": TABLE_AUDIT,
        "cols":         AUDIT_LOG_COLS,
        "time_field":   "SK",
        "time_prefix":  "TS#",       # SK = "TS#2026-07-01T..." → strip prefix ao retornar
    },
    "app_logs": {
        "dynamo_table": TABLE_APPLOGS,
        "cols":         APP_LOGS_COLS,
        "time_field":   "Timestamp",
        "time_prefix":  "",
    },
}

# col_sql → (campo_dynamo, tipo_dynamo) — lookup rápido por tabela
COL_MAP: dict[str, dict] = {
    tname: {c[0]: (c[1], c[3]) for c in info["cols"]}
    for tname, info in TABLES.items()
}

# ── Cliente DynamoDB ────────────────────────────────────────────────────────
dynamo = boto3.client(
    "dynamodb",
    endpoint_url=DYNAMO_ENDPOINT,
    region_name=DYNAMO_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
)


# ════════════════════════════════════════════════════════════════════════════
# DynamoDB
# ════════════════════════════════════════════════════════════════════════════

def scan_dynamo(table_name: str, conditions: list[dict]) -> list[dict]:
    """
    Executa um Scan no DynamoDB com FilterExpression derivado do WHERE SQL.
    Pagina automaticamente para retornar todos os registros.
    """
    info    = TABLES[table_name]
    col_map = COL_MAP[table_name]

    filter_parts: list[str] = []
    attr_names:   dict      = {}
    attr_values:  dict      = {}

    for i, cond in enumerate(conditions):
        col = cond["col"]
        if col not in col_map:
            continue

        dynamo_field, val_type = col_map[col]
        nk = f"#n{i}"
        vk = f":v{i}"
        attr_names[nk] = dynamo_field

        # Para timestamp no audit_log, o DynamoDB armazena com prefixo "TS#"
        val = cond["val"]
        if dynamo_field == info["time_field"] and info["time_prefix"]:
            val = info["time_prefix"] + val

        attr_values[vk] = {"N": str(val)} if val_type == "N" else {"S": str(val)}

        op = cond["op"]
        if   op == "=":        filter_parts.append(f"{nk} = {vk}")
        elif op == ">=":       filter_parts.append(f"{nk} >= {vk}")
        elif op == "<=":       filter_parts.append(f"{nk} <= {vk}")
        elif op == ">":        filter_parts.append(f"{nk} > {vk}")
        elif op == "<":        filter_parts.append(f"{nk} < {vk}")
        elif op == "contains": filter_parts.append(f"contains({nk}, {vk})")

    kwargs: dict = {"TableName": info["dynamo_table"]}
    if filter_parts:
        kwargs["FilterExpression"]         = " AND ".join(filter_parts)
        kwargs["ExpressionAttributeNames"] = attr_names
        kwargs["ExpressionAttributeValues"]= attr_values

    items: list[dict] = []
    while True:
        resp = dynamo.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    log.info(f"[{table_name}] {len(items)} registro(s) retornado(s) | filtros: {len(filter_parts)}")
    return items


def item_to_row(item: dict, cols: list[tuple], time_prefix: str) -> list:
    """Converte um item DynamoDB em uma linha de valores PostgreSQL."""
    row = []
    for _col_sql, dynamo_field, _oid, val_type in cols:
        cell = item.get(dynamo_field, {})
        val  = cell.get("S") or cell.get("N") or None
        # Remove prefixo do timestamp (ex: "TS#2026-07-01..." → "2026-07-01...")
        if val and time_prefix and val.startswith(time_prefix):
            val = val[len(time_prefix):]
        row.append(val)
    return row


# ════════════════════════════════════════════════════════════════════════════
# SQL Parser
# ════════════════════════════════════════════════════════════════════════════

def parse_where(where_str: str) -> list[dict]:
    """
    Converte uma cláusula WHERE SQL em lista de condições para o DynamoDB.
    Suporta: =, >=, <=, >, <, LIKE, BETWEEN ... AND ...
    Remove macros do Grafana ($__timeFilter, $__timeGroup).
    """
    if not where_str:
        return []

    # Remove macros Grafana
    s = re.sub(r"\$__timeFilter\s*\([^)]*\)",  "1=1", where_str, flags=re.IGNORECASE)
    s = re.sub(r"\$__timeGroup\s*\([^)]*\)",   "1=1", s,         flags=re.IGNORECASE)
    s = re.sub(r"\$__timeTo\s*\([^)]*\)",      "1=1", s,         flags=re.IGNORECASE)
    s = re.sub(r"\$__timeFrom\s*\([^)]*\)",    "1=1", s,         flags=re.IGNORECASE)

    conds: list[dict] = []

    # Divide por AND (não aninhado)
    for part in re.split(r"\bAND\b", s, flags=re.IGNORECASE):
        part = part.strip().strip("()")
        if not part or part == "1=1":
            continue

        # BETWEEN col BETWEEN 'a' AND 'b'
        m = re.match(
            r'"?(\w+)"?\s+BETWEEN\s+\'?([^\']+?)\'?\s+AND\s+\'?([^\']+?)\'?$',
            part, re.IGNORECASE
        )
        if m:
            col = m.group(1).lower()
            conds.append({"col": col, "op": ">=", "val": m.group(2).strip().strip("'")})
            conds.append({"col": col, "op": "<=", "val": m.group(3).strip().strip("'")})
            continue

        # LIKE
        m = re.match(r'"?(\w+)"?\s+LIKE\s+\'([^\']+)\'', part, re.IGNORECASE)
        if m:
            val = m.group(2).replace("%", "")
            conds.append({"col": m.group(1).lower(), "op": "contains", "val": val})
            continue

        # Operadores padrão =, >=, <=, >, <
        m = re.match(
            r'"?(\w+)"?\s*(=|>=|<=|>|<)\s*\'?([^\']+?)\'?\s*$',
            part
        )
        if m:
            conds.append({
                "col": m.group(1).lower(),
                "op":  m.group(2),
                "val": m.group(3).strip().strip("'"),
            })

    return conds


def parse_sql(sql: str) -> dict:
    """
    Faz o parse do SQL recebido e retorna um dict com o tipo e os parâmetros.
    Tipos: empty | command | const | select
    """
    sql   = sql.strip().rstrip(";").strip()
    upper = sql.upper()

    if not sql:
        return {"type": "empty"}

    # Comandos sem retorno de dados
    if re.match(
        r"^(SET|SHOW|BEGIN|COMMIT|ROLLBACK|DEALLOCATE|DISCARD|RESET|CLOSE|DECLARE|FETCH)",
        upper
    ):
        return {"type": "command"}

    # Queries de sistema com resposta fixa
    if re.match(r"SELECT\s+1\b", sql, re.IGNORECASE):
        return {"type": "const", "cols": [("?column?", OID_INT4)], "rows": [["1"]]}

    if re.match(r"SELECT\s+version\s*\(\s*\)", sql, re.IGNORECASE):
        return {"type": "const", "cols": [("version", OID_TEXT)],
                "rows": [["PostgreSQL 14.0 (DynamoDB Proxy — Conexão Solidária)"]]}

    if re.match(r"SELECT\s+current_schema\s*\(\s*\)", sql, re.IGNORECASE):
        return {"type": "const", "cols": [("current_schema", OID_TEXT)], "rows": [["public"]]}

    if re.match(r"SELECT\s+current_database\s*\(\s*\)", sql, re.IGNORECASE):
        return {"type": "const", "cols": [("current_database", OID_TEXT)], "rows": [["dynamo"]]}

    # information_schema.tables → lista as tabelas disponíveis
    if "information_schema.tables" in sql.lower():
        return {
            "type": "const",
            "cols": [("table_name", OID_TEXT), ("table_schema", OID_TEXT), ("table_type", OID_TEXT)],
            "rows": [
                ["audit_log", "public", "BASE TABLE"],
                ["app_logs",  "public", "BASE TABLE"],
            ],
        }

    # information_schema.columns → colunas de uma tabela
    m = re.search(r"information_schema\.columns.*?table_name\s*=\s*'(\w+)'", sql, re.IGNORECASE | re.DOTALL)
    if m:
        tname = m.group(1).lower()
        rows  = [[c[0], "text"] for c in TABLES[tname]["cols"]] if tname in TABLES else []
        return {
            "type": "const",
            "cols": [("column_name", OID_TEXT), ("data_type", OID_TEXT)],
            "rows": rows,
        }

    # pg_catalog queries → ignorar
    if "pg_catalog" in sql.lower() or "pg_type" in sql.lower():
        return {"type": "command"}

    # COUNT(*) — retorna o total de registros
    m = re.match(
        r"SELECT\s+COUNT\s*\(\s*\*\s*\)\s+AS\s+\w+\s+FROM\s+\"?(\w+)\"?"
        r"(?:\s+WHERE\s+(.+?))?(?:\s+ORDER\s+BY\s+.+?)?(?:\s+LIMIT\s+\d+)?\s*$",
        sql, re.IGNORECASE | re.DOTALL
    )
    if m:
        return {
            "type":  "count",
            "table": m.group(1).lower(),
            "where": parse_where(m.group(2) or ""),
        }

    # SELECT principal
    m = re.match(
        r"SELECT\s+(.+?)\s+FROM\s+\"?(\w+)\"?"
        r"(?:\s+WHERE\s+(.+?))?"
        r"(?:\s+ORDER\s+BY\s+.+?)?"
        r"(?:\s+LIMIT\s+(\d+))?"
        r"\s*$",
        sql, re.IGNORECASE | re.DOTALL
    )
    if m:
        return {
            "type":     "select",
            "cols_str": m.group(1).strip(),
            "table":    m.group(2).lower(),
            "where":    parse_where(m.group(3) or ""),
            "limit":    int(m.group(4)) if m.group(4) else None,
        }

    return {"type": "command"}


# ════════════════════════════════════════════════════════════════════════════
# PostgreSQL Wire Protocol v3
# ════════════════════════════════════════════════════════════════════════════

def pg_msg(typ: bytes, payload: bytes) -> bytes:
    """Empacota uma mensagem PostgreSQL: tipo(1) + tamanho(4) + payload."""
    return typ + struct.pack("!I", len(payload) + 4) + payload

def pg_auth_ok()         -> bytes: return pg_msg(b"R", struct.pack("!I", 0))
def pg_ready_for_query() -> bytes: return pg_msg(b"Z", b"I")
def pg_parse_complete()  -> bytes: return pg_msg(b"1", b"")
def pg_bind_complete()   -> bytes: return pg_msg(b"2", b"")
def pg_close_complete()  -> bytes: return pg_msg(b"3", b"")
def pg_no_data()         -> bytes: return pg_msg(b"n", b"")
def pg_empty_query()     -> bytes: return pg_msg(b"I", b"")

def pg_command_complete(tag: str) -> bytes:
    return pg_msg(b"C", (tag + "\x00").encode())

def pg_param_status(k: str, v: str) -> bytes:
    return pg_msg(b"S", (k + "\x00" + v + "\x00").encode())

def pg_backend_key_data() -> bytes:
    return pg_msg(b"K", struct.pack("!II", 1, 0))

def pg_error(msg: str, code: str = "42601") -> bytes:
    payload  = b"S" + b"ERROR\x00"
    payload += b"C" + code.encode() + b"\x00"
    payload += b"M" + msg.encode("utf-8") + b"\x00"
    payload += b"\x00"
    return pg_msg(b"E", payload)

def pg_row_description(cols: list[tuple]) -> bytes:
    """cols: [(nome, oid), ...]"""
    body = struct.pack("!H", len(cols))
    for name, oid in cols:
        body += name.encode() + b"\x00"
        # tableOID(I) + colAttr(H) + typeOID(I) + typeSize(h) + typeMod(i) + fmt(H)
        # typeSize e typeMod podem ser -1, por isso h e i (signed)
        body += struct.pack("!IHIhiH", 0, 0, oid, -1, -1, 0)
    return pg_msg(b"T", body)

def pg_data_row(values: list) -> bytes:
    body = struct.pack("!H", len(values))
    for v in values:
        if v is None:
            body += struct.pack("!i", -1)
        else:
            enc   = str(v).encode("utf-8")
            body += struct.pack("!I", len(enc)) + enc
    return pg_msg(b"D", body)


# ════════════════════════════════════════════════════════════════════════════
# Query handler
# ════════════════════════════════════════════════════════════════════════════

async def handle_query(sql: str, writer: asyncio.StreamWriter) -> None:
    log.info(f"SQL: {sql[:300]}")
    parsed = parse_sql(sql)
    t      = parsed["type"]

    if t == "empty":
        writer.write(pg_empty_query())
        writer.write(pg_ready_for_query())

    elif t == "command":
        writer.write(pg_command_complete("OK"))
        writer.write(pg_ready_for_query())

    elif t == "const":
        writer.write(pg_row_description(parsed["cols"]))
        for row in parsed["rows"]:
            writer.write(pg_data_row(row))
        writer.write(pg_command_complete(f"SELECT {len(parsed['rows'])}"))
        writer.write(pg_ready_for_query())

    elif t == "count":
        table_name = parsed["table"]
        if table_name not in TABLES:
            writer.write(pg_error(f'relation "{table_name}" does not exist', "42P01"))
            writer.write(pg_ready_for_query())
            await writer.drain()
            return
        try:
            items = scan_dynamo(table_name, parsed["where"])
        except Exception as e:
            log.error(f"DynamoDB error: {e}", exc_info=True)
            writer.write(pg_error(str(e)))
            writer.write(pg_ready_for_query())
            await writer.drain()
            return
        writer.write(pg_row_description([("total", OID_INT4)]))
        writer.write(pg_data_row([str(len(items))]))
        writer.write(pg_command_complete("SELECT 1"))
        writer.write(pg_ready_for_query())

    elif t == "select":
        table_name = parsed["table"]
        if table_name not in TABLES:
            writer.write(pg_error(f'relation "{table_name}" does not exist', "42P01"))
            writer.write(pg_ready_for_query())
            await writer.drain()
            return

        info        = TABLES[table_name]
        all_cols    = info["cols"]
        time_prefix = info["time_prefix"]
        cols_str    = parsed["cols_str"]

        # Resolve colunas requisitadas
        if cols_str.strip() == "*":
            result_cols = all_cols
        else:
            requested   = {c.strip().strip('"').lower() for c in cols_str.split(",")}
            result_cols = [c for c in all_cols if c[0] in requested] or all_cols

        try:
            items = scan_dynamo(table_name, parsed["where"])
        except Exception as e:
            log.error(f"DynamoDB error: {e}", exc_info=True)
            writer.write(pg_error(str(e)))
            writer.write(pg_ready_for_query())
            await writer.drain()
            return

        limit = parsed.get("limit")
        if limit:
            items = items[:limit]

        writer.write(pg_row_description([(c[0], c[2]) for c in result_cols]))
        for item in items:
            row = item_to_row(item, result_cols, time_prefix)
            writer.write(pg_data_row(row))
        writer.write(pg_command_complete(f"SELECT {len(items)}"))
        writer.write(pg_ready_for_query())

    await writer.drain()


# ════════════════════════════════════════════════════════════════════════════
# Conexão TCP
# ════════════════════════════════════════════════════════════════════════════

async def client_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    log.info(f"Conexão: {peer}")

    try:
        # ── Startup ──────────────────────────────────────────────────────────
        raw = await reader.readexactly(4)
        length = struct.unpack("!I", raw)[0]
        startup = await reader.readexactly(length - 4)

        # Recusa SSL e lê o startup de verdade
        if len(startup) >= 4 and struct.unpack("!I", startup[:4])[0] == 80877103:
            writer.write(b"N")
            await writer.drain()
            raw     = await reader.readexactly(4)
            length  = struct.unpack("!I", raw)[0]
            startup = await reader.readexactly(length - 4)

        # Responde ao startup sem exigir senha
        writer.write(pg_auth_ok())
        for k, v in [
            ("server_version",              "14.0"),
            ("client_encoding",             "UTF8"),
            ("DateStyle",                   "ISO, MDY"),
            ("integer_datetimes",           "on"),
            ("standard_conforming_strings", "on"),
        ]:
            writer.write(pg_param_status(k, v))
        writer.write(pg_backend_key_data())
        writer.write(pg_ready_for_query())
        await writer.drain()

        # ── Loop de mensagens ─────────────────────────────────────────────────
        while True:
            type_b  = await reader.readexactly(1)
            raw     = await reader.readexactly(4)
            msg_len = struct.unpack("!I", raw)[0]
            payload = await reader.readexactly(msg_len - 4)

            if type_b == b"X":   # Terminate
                break

            elif type_b == b"Q":  # Simple Query
                sql = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    await handle_query(stmt, writer)

            elif type_b == b"P":  # Parse  (prepared statements)
                writer.write(pg_parse_complete())
                await writer.drain()

            elif type_b == b"B":  # Bind
                writer.write(pg_bind_complete())
                await writer.drain()

            elif type_b == b"D":  # Describe
                writer.write(pg_no_data())
                await writer.drain()

            elif type_b == b"E":  # Execute
                writer.write(pg_command_complete("SELECT 0"))
                writer.write(pg_ready_for_query())
                await writer.drain()

            elif type_b == b"S":  # Sync
                writer.write(pg_ready_for_query())
                await writer.drain()

            elif type_b == b"C":  # Close
                writer.write(pg_close_complete())
                await writer.drain()

            else:
                log.debug(f"Mensagem desconhecida: {type_b} — ignorada")

    except asyncio.IncompleteReadError:
        pass
    except Exception as e:
        log.error(f"Erro em {peer}: {e}", exc_info=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        log.info(f"Conexão encerrada: {peer}")


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    log.info(f"DynamoDB PostgreSQL Proxy | {PG_HOST}:{PG_PORT}")
    log.info(f"DynamoDB: {DYNAMO_ENDPOINT}")
    log.info(f"Tabelas: {TABLE_AUDIT} → audit_log | {TABLE_APPLOGS} → app_logs")

    server = await asyncio.start_server(client_handler, PG_HOST, PG_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())