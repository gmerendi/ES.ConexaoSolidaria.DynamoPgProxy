"""
Testes unitários do DynamoDB PostgreSQL Proxy.
Cobre: parse_sql, parse_where, pg_row_description, pg_data_row,
       item_to_row e scan_dynamo (com DynamoDB mockado).
"""

import struct
import unittest
from unittest.mock import MagicMock, patch

# Importa o módulo a testar (sem executar o main)
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pg_dynamo_proxy as proxy


# ══════════════════════════════════════════════════════════════════════════════
# parse_where
# ══════════════════════════════════════════════════════════════════════════════

class TestParseWhere(unittest.TestCase):

    def test_igualdade_simples(self):
        conds = proxy.parse_where("service = 'CS-CAMPANHAS-API'")
        self.assertEqual(len(conds), 1)
        self.assertEqual(conds[0]["col"], "service")
        self.assertEqual(conds[0]["op"],  "=")
        self.assertEqual(conds[0]["val"], "CS-CAMPANHAS-API")

    def test_multiplas_condicoes_and(self):
        conds = proxy.parse_where("service = 'SVC' AND operation = 'ADDED'")
        self.assertEqual(len(conds), 2)
        self.assertEqual(conds[0]["col"], "service")
        self.assertEqual(conds[1]["col"], "operation")

    def test_between(self):
        conds = proxy.parse_where(
            "timestamp BETWEEN '2026-01-01T00:00:00Z' AND '2026-12-31T23:59:59Z'"
        )
        self.assertEqual(len(conds), 2)
        self.assertEqual(conds[0]["op"], ">=")
        self.assertEqual(conds[1]["op"], "<=")
        self.assertEqual(conds[0]["val"], "2026-01-01T00:00:00Z")
        self.assertEqual(conds[1]["val"], "2026-12-31T23:59:59Z")

    def test_maior_igual(self):
        conds = proxy.parse_where("timestamp >= '2026-06-01T00:00:00Z'")
        self.assertEqual(conds[0]["op"], ">=")

    def test_menor_igual(self):
        conds = proxy.parse_where("timestamp <= '2026-06-30T23:59:59Z'")
        self.assertEqual(conds[0]["op"], "<=")

    def test_like(self):
        conds = proxy.parse_where("message LIKE '%login%'")
        self.assertEqual(conds[0]["op"],  "contains")
        self.assertEqual(conds[0]["val"], "login")

    def test_grafana_timefilter_ignorado(self):
        conds = proxy.parse_where("$__timeFilter(timestamp) AND service = 'X'")
        self.assertEqual(len(conds), 1)
        self.assertEqual(conds[0]["col"], "service")

    def test_vazio(self):
        self.assertEqual(proxy.parse_where(""), [])

    def test_coluna_com_aspas(self):
        conds = proxy.parse_where('"service" = \'CS-USUARIOS-API\'')
        self.assertEqual(conds[0]["col"], "service")
        self.assertEqual(conds[0]["val"], "CS-USUARIOS-API")


# ══════════════════════════════════════════════════════════════════════════════
# parse_sql
# ══════════════════════════════════════════════════════════════════════════════

class TestParseSql(unittest.TestCase):

    def test_select_all(self):
        r = proxy.parse_sql("SELECT * FROM audit_log")
        self.assertEqual(r["type"],  "select")
        self.assertEqual(r["table"], "audit_log")
        self.assertEqual(r["cols_str"], "*")
        self.assertEqual(r["where"], [])

    def test_select_com_where(self):
        r = proxy.parse_sql(
            "SELECT timestamp, service FROM audit_log WHERE service = 'SVC'"
        )
        self.assertEqual(r["type"],  "select")
        self.assertEqual(r["table"], "audit_log")
        self.assertIn("timestamp", r["cols_str"])
        self.assertEqual(len(r["where"]), 1)

    def test_select_com_limit(self):
        r = proxy.parse_sql("SELECT * FROM audit_log LIMIT 10")
        self.assertEqual(r["limit"], 10)

    def test_select_com_order_by(self):
        r = proxy.parse_sql("SELECT * FROM audit_log ORDER BY timestamp DESC")
        self.assertEqual(r["type"], "select")

    def test_select_1(self):
        r = proxy.parse_sql("SELECT 1")
        self.assertEqual(r["type"], "const")
        self.assertEqual(r["rows"], [["1"]])

    def test_select_version(self):
        r = proxy.parse_sql("SELECT version()")
        self.assertEqual(r["type"], "const")
        self.assertIn("PostgreSQL", r["rows"][0][0])

    def test_set_ignorado(self):
        r = proxy.parse_sql("SET client_encoding TO 'UTF8'")
        self.assertEqual(r["type"], "command")

    def test_show_ignorado(self):
        r = proxy.parse_sql("SHOW server_version")
        self.assertEqual(r["type"], "command")

    def test_vazio(self):
        r = proxy.parse_sql("")
        self.assertEqual(r["type"], "empty")

    def test_vazio_ponto_virgula(self):
        r = proxy.parse_sql(";")
        self.assertEqual(r["type"], "empty")

    def test_information_schema_tables(self):
        r = proxy.parse_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        self.assertEqual(r["type"], "const")
        nomes = [row[0] for row in r["rows"]]
        self.assertIn("audit_log", nomes)
        self.assertIn("app_logs",  nomes)

    def test_information_schema_columns_audit_log(self):
        r = proxy.parse_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit_log'"
        )
        self.assertEqual(r["type"], "const")
        colunas = [row[0] for row in r["rows"]]
        self.assertIn("timestamp", colunas)
        self.assertIn("service",   colunas)
        self.assertIn("operation", colunas)

    def test_information_schema_columns_app_logs(self):
        r = proxy.parse_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'app_logs'"
        )
        colunas = [row[0] for row in r["rows"]]
        self.assertIn("level",          colunas)
        self.assertIn("correlation_id", colunas)

    def test_count(self):
        r = proxy.parse_sql("SELECT COUNT(*) AS total FROM audit_log")
        self.assertEqual(r["type"],  "count")
        self.assertEqual(r["table"], "audit_log")

    def test_count_com_where(self):
        r = proxy.parse_sql(
            "SELECT COUNT(*) AS total FROM audit_log WHERE operation = 'ADDED'"
        )
        self.assertEqual(r["type"], "count")
        self.assertEqual(len(r["where"]), 1)

    def test_distinct(self):
        r = proxy.parse_sql("SELECT DISTINCT service FROM audit_log ORDER BY 1")
        self.assertEqual(r["type"],  "distinct")
        self.assertEqual(r["col"],   "service")
        self.assertEqual(r["table"], "audit_log")

    def test_distinct_app_logs(self):
        r = proxy.parse_sql("SELECT DISTINCT level FROM app_logs ORDER BY 1")
        self.assertEqual(r["type"],  "distinct")
        self.assertEqual(r["col"],   "level")
        self.assertEqual(r["table"], "app_logs")

    def test_tabela_desconhecida_ainda_retorna_select(self):
        r = proxy.parse_sql("SELECT * FROM tabela_inexistente")
        self.assertEqual(r["type"],  "select")
        self.assertEqual(r["table"], "tabela_inexistente")


# ══════════════════════════════════════════════════════════════════════════════
# PostgreSQL Wire Protocol
# ══════════════════════════════════════════════════════════════════════════════

class TestPgProtocol(unittest.TestCase):

    def _decode_msg(self, data: bytes):
        """Extrai tipo, tamanho e payload de uma mensagem PG."""
        msg_type = data[0:1]
        length   = struct.unpack("!I", data[1:5])[0]
        payload  = data[5:5 + length - 4]
        return msg_type, length, payload

    def test_pg_auth_ok(self):
        msg = proxy.pg_auth_ok()
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"R")
        self.assertEqual(struct.unpack("!I", payload)[0], 0)

    def test_pg_ready_for_query(self):
        msg = proxy.pg_ready_for_query()
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"Z")
        self.assertEqual(payload, b"I")

    def test_pg_command_complete(self):
        msg = proxy.pg_command_complete("SELECT 5")
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"C")
        self.assertEqual(payload, b"SELECT 5\x00")

    def test_pg_row_description_uma_coluna(self):
        msg = proxy.pg_row_description([("service", proxy.OID_TEXT)])
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"T")
        num_cols = struct.unpack("!H", payload[:2])[0]
        self.assertEqual(num_cols, 1)
        self.assertIn(b"service", payload)

    def test_pg_row_description_multiplas_colunas(self):
        cols = [
            ("timestamp",  proxy.OID_TEXT),
            ("service",    proxy.OID_TEXT),
            ("operation",  proxy.OID_TEXT),
        ]
        msg = proxy.pg_row_description(cols)
        _, _, payload = self._decode_msg(msg)
        num_cols = struct.unpack("!H", payload[:2])[0]
        self.assertEqual(num_cols, 3)

    def test_pg_data_row_valores_texto(self):
        msg = proxy.pg_data_row(["CS-CAMPANHAS-API", "ADDED"])
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"D")
        num_cols = struct.unpack("!H", payload[:2])[0]
        self.assertEqual(num_cols, 2)
        self.assertIn(b"CS-CAMPANHAS-API", payload)
        self.assertIn(b"ADDED", payload)

    def test_pg_data_row_valor_none(self):
        msg = proxy.pg_data_row([None, "valor"])
        _, _, payload = self._decode_msg(msg)
        # Primeiro campo None → tamanho -1 (0xFFFFFFFF como signed)
        offset = 2  # pula num_cols
        length = struct.unpack("!i", payload[offset:offset + 4])[0]
        self.assertEqual(length, -1)

    def test_pg_error(self):
        msg = proxy.pg_error("relation does not exist", "42P01")
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"E")
        self.assertIn(b"relation does not exist", payload)

    def test_pg_param_status(self):
        msg = proxy.pg_param_status("server_version", "14.0")
        t, _, payload = self._decode_msg(msg)
        self.assertEqual(t, b"S")
        self.assertIn(b"server_version", payload)
        self.assertIn(b"14.0", payload)

    def test_pg_parse_complete(self):
        msg = proxy.pg_parse_complete()
        t, _, _ = self._decode_msg(msg)
        self.assertEqual(t, b"1")

    def test_pg_bind_complete(self):
        msg = proxy.pg_bind_complete()
        t, _, _ = self._decode_msg(msg)
        self.assertEqual(t, b"2")

    def test_pg_empty_query(self):
        msg = proxy.pg_empty_query()
        t, _, _ = self._decode_msg(msg)
        self.assertEqual(t, b"I")


# ══════════════════════════════════════════════════════════════════════════════
# item_to_row
# ══════════════════════════════════════════════════════════════════════════════

class TestItemToRow(unittest.TestCase):

    def test_audit_log_item_completo(self):
        item = {
            "SK":          {"S": "TS#2026-07-01T11:25:00Z"},
            "ServiceName": {"S": "CS-CAMPANHAS-API"},
            "Operation":   {"S": "ADDED"},
            "ChangedBy":   {"S": "admin@conexao-solidaria.com.br"},
            "ResourceId":  {"S": "abc-123"},
            "IpAddress":   {"S": "10.0.0.1"},
            "PK":          {"S": "ENTITY#CAMPANHA#abc-123"},
            "Payload":     {"S": '{"titulo": "Campanha 1"}'},
        }
        cols = proxy.TABLES["audit_log"]["cols"]
        row  = proxy.item_to_row(item, cols, "TS#")

        self.assertEqual(row[0], "2026-07-01T11:25:00Z")  # prefixo removido
        self.assertEqual(row[1], "CS-CAMPANHAS-API")
        self.assertEqual(row[2], "ADDED")
        self.assertEqual(row[3], "admin@conexao-solidaria.com.br")

    def test_app_logs_item_completo(self):
        item = {
            "Timestamp":     {"S": "2026-07-01T13:14:49Z"},
            "LogLevel":      {"S": "Information"},
            "Caller":        {"S": "Users.API.Controllers.AuthController"},
            "Message":       {"S": "Iniciando login"},
            "CorrelationId": {"S": "4e7f3406-48a2-434b-8cdf-5e8d439e9e9e"},
            "Data":          {"S": '"admin@example.com"'},
            "Type":          {"N": "1"},
        }
        cols = proxy.TABLES["app_logs"]["cols"]
        row  = proxy.item_to_row(item, cols, "")

        self.assertEqual(row[0], "2026-07-01T13:14:49Z")
        self.assertEqual(row[1], "Information")
        self.assertEqual(row[2], "Users.API.Controllers.AuthController")
        self.assertEqual(row[4], "4e7f3406-48a2-434b-8cdf-5e8d439e9e9e")

    def test_campo_ausente_retorna_none(self):
        item = {"SK": {"S": "TS#2026-07-01T00:00:00Z"}}
        cols = proxy.TABLES["audit_log"]["cols"]
        row  = proxy.item_to_row(item, cols, "TS#")
        self.assertIsNone(row[1])  # ServiceName ausente

    def test_prefixo_removido_apenas_do_sk(self):
        item = {
            "SK":          {"S": "TS#2026-07-01T00:00:00Z"},
            "ServiceName": {"S": "CS-TEST"},
        }
        cols = proxy.TABLES["audit_log"]["cols"]
        row  = proxy.item_to_row(item, cols, "TS#")
        self.assertFalse(row[0].startswith("TS#"))
        self.assertEqual(row[1], "CS-TEST")


# ══════════════════════════════════════════════════════════════════════════════
# scan_dynamo (com mock)
# ══════════════════════════════════════════════════════════════════════════════

class TestScanDynamo(unittest.TestCase):

    def _make_item(self, service: str, operation: str) -> dict:
        return {
            "SK":          {"S": f"TS#2026-07-01T00:00:00Z"},
            "ServiceName": {"S": service},
            "Operation":   {"S": operation},
            "ChangedBy":   {"S": "admin@test.com"},
            "ResourceId":  {"S": "res-123"},
            "IpAddress":   {"S": "10.0.0.1"},
            "PK":          {"S": f"ENTITY#TEST#res-123"},
            "Payload":     {"S": "{}"},
        }

    def test_scan_sem_filtros(self):
        itens = [
            self._make_item("CS-CAMPANHAS-API", "ADDED"),
            self._make_item("CS-USUARIOS-API",  "MODIFIED"),
        ]
        proxy.dynamo.scan = MagicMock(return_value={"Items": itens})

        resultado = proxy.scan_dynamo("audit_log", [])
        self.assertEqual(len(resultado), 2)

    def test_scan_com_filtro_por_service(self):
        """Verifica que o FilterExpression é montado corretamente."""
        proxy.dynamo.scan = MagicMock(return_value={"Items": []})

        proxy.scan_dynamo("audit_log", [
            {"col": "service", "op": "=", "val": "CS-CAMPANHAS-API"}
        ])

        call_kwargs = proxy.dynamo.scan.call_args[1]
        self.assertIn("FilterExpression",          call_kwargs)
        self.assertIn("ExpressionAttributeNames",  call_kwargs)
        self.assertIn("ExpressionAttributeValues", call_kwargs)
        self.assertIn("CS-CAMPANHAS-API",
                      str(call_kwargs["ExpressionAttributeValues"]))

    def test_scan_com_filtro_timestamp_adiciona_prefixo(self):
        """Para audit_log, filtro de timestamp deve incluir o prefixo TS#."""
        proxy.dynamo.scan = MagicMock(return_value={"Items": []})

        proxy.scan_dynamo("audit_log", [
            {"col": "timestamp", "op": ">=", "val": "2026-06-01T00:00:00Z"}
        ])

        call_kwargs = proxy.dynamo.scan.call_args[1]
        valores = str(call_kwargs["ExpressionAttributeValues"])
        self.assertIn("TS#2026-06-01T00:00:00Z", valores)

    def test_scan_app_logs_sem_prefixo_no_timestamp(self):
        """Para app_logs, timestamp não tem prefixo."""
        proxy.dynamo.scan = MagicMock(return_value={"Items": []})

        proxy.scan_dynamo("app_logs", [
            {"col": "timestamp", "op": ">=", "val": "2026-06-01T00:00:00Z"}
        ])

        call_kwargs = proxy.dynamo.scan.call_args[1]
        valores = str(call_kwargs["ExpressionAttributeValues"])
        self.assertIn("2026-06-01T00:00:00Z", valores)
        self.assertNotIn("TS#", valores)

    def test_scan_paginado(self):
        """Verifica que o scan pagina corretamente."""
        item = self._make_item("CS-TEST", "ADDED")
        proxy.dynamo.scan = MagicMock(side_effect=[
            {"Items": [item], "LastEvaluatedKey": {"SK": {"S": "TS#x"}}},
            {"Items": [item]},
        ])

        resultado = proxy.scan_dynamo("audit_log", [])
        self.assertEqual(len(resultado), 2)
        self.assertEqual(proxy.dynamo.scan.call_count, 2)

    def test_scan_tabela_desconhecida_retorna_vazio(self):
        resultado = proxy.scan_dynamo("tabela_que_nao_existe", [])
        self.assertEqual(resultado, [])

    def test_scan_filtro_coluna_desconhecida_ignorada(self):
        """Coluna que não existe no schema deve ser ignorada silenciosamente."""
        proxy.dynamo.scan = MagicMock(return_value={"Items": []})

        proxy.scan_dynamo("audit_log", [
            {"col": "coluna_inexistente", "op": "=", "val": "X"}
        ])

        call_kwargs = proxy.dynamo.scan.call_args[1]
        self.assertNotIn("FilterExpression", call_kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
