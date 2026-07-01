# ES.ConexaoSolidaria.DynamoPgProxy

Serviço Python que implementa o **protocolo wire do PostgreSQL** e traduz
as queries SQL diretamente para o **DynamoDB** — sem duplicação de dados,
sem camadas extras, sem plugins no Grafana.

```
Grafana (datasource PostgreSQL nativo)
    ↓ SQL
cs-dynamo-pg-proxy:5450
    ↓ DynamoDB Scan com FilterExpression
DynamoDB Local (cs-audit-log / cs-app-logs)
```

---

## Tabelas disponíveis

| Tabela SQL   | Tabela DynamoDB | Descrição               |
|--------------|-----------------|-------------------------|
| `audit_log`  | `cs-audit-log`  | Audit trail do sistema  |
| `app_logs`   | `cs-app-logs`   | Logs de aplicação/trace |

### Colunas — audit_log

| Coluna        | Campo DynamoDB | Tipo |
|---------------|----------------|------|
| `timestamp`   | `SK`           | text |
| `service`     | `ServiceName`  | text |
| `operation`   | `Operation`    | text |
| `changed_by`  | `ChangedBy`    | text |
| `resource_id` | `ResourceId`   | text |
| `ip_address`  | `IpAddress`    | text |
| `pk`          | `PK`           | text |
| `payload`     | `Payload`      | text |

### Colunas — app_logs

| Coluna           | Campo DynamoDB  | Tipo |
|------------------|-----------------|------|
| `timestamp`      | `Timestamp`     | text |
| `level`          | `LogLevel`      | text |
| `caller`         | `Caller`        | text |
| `message`        | `Message`       | text |
| `correlation_id` | `CorrelationId` | text |
| `data`           | `Data`          | text |
| `type`           | `Type`          | int  |

---

## Rodar standalone

```bash
# Ajuste o .env se necessário
docker compose up --build
```

O proxy fica disponível em `localhost:5450`.

---

## Integrar à infra

Adicione ao `docker-compose.yml` da infra:

```yaml
  cs.dynamo.pg.proxy:
    image: ${DOCKER_REGISTRY-}cs-dynamo-pg-proxy:latest
    build:
      context: ../../ES.ConexaoSolidaria.DynamoPgProxy
      dockerfile: Dockerfile
    container_name: cs-dynamo-pg-proxy
    ports:
      - "5450:5450"
    environment:
      DYNAMO_ENDPOINT:      "http://cs-dynamo-db:8000"
      DYNAMO_REGION:        "us-east-1"
      DYNAMO_TABLE_AUDIT:   "cs-audit-log"
      DYNAMO_TABLE_APPLOGS: "cs-app-logs"
      AWS_ACCESS_KEY_ID:    "AKIAIOSFODNN7EXAMPLE"
      AWS_SECRET_ACCESS_KEY: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    depends_on:
      cs.dynamo.db:
        condition: service_healthy
    networks:
      - csnetwork
    restart: on-failure
```

E o datasource no `grafana_datasource`:

```yaml
        - name: DynamoDB-PG
          uid: dynamo-pg
          type: postgres
          url: cs-dynamo-pg-proxy:5450
          user: grafana
          database: dynamo
          isDefault: false
          editable: false
          jsonData:
            sslmode: disable
            postgresVersion: 1400
          secureJsonData:
            password: grafana
```

---

## Dashboards

Copie os JSONs para `observability/grafana/provisioning/dashboards/`:

- `cs-audit-log.json` — Audit Log com filtros de serviço e operação
- `cs-app-logs.json`  — Application Logs com filtro de Correlation ID (trace)

---

## Queries SQL suportadas

```sql
-- Todos os registros
SELECT * FROM audit_log

-- Filtrar por serviço e operação
SELECT * FROM audit_log
WHERE service = 'CS-CAMPANHAS-API'
AND operation = 'ADDED'

-- Filtrar por intervalo de data (audit_log)
SELECT * FROM audit_log
WHERE timestamp >= '2026-06-01T00:00:00Z'
AND timestamp <= '2026-06-30T23:59:59Z'

-- Trace completo por CorrelationId
SELECT * FROM app_logs
WHERE correlation_id = '4e7f3406-48a2-434b-8cdf-5e8d439e9e9e'
ORDER BY timestamp ASC

-- Filtrar logs de erro
SELECT * FROM app_logs
WHERE level = 'Error'

-- Busca parcial (LIKE)
SELECT * FROM app_logs
WHERE message LIKE '%login%'

-- Agrupamento
SELECT service, COUNT(*) AS total FROM audit_log GROUP BY service

-- Tabelas disponíveis
SELECT table_name FROM information_schema.tables
```

---

## Variáveis de ambiente

| Variável               | Padrão                    | Descrição                          |
|------------------------|---------------------------|------------------------------------|
| `PG_HOST`              | `0.0.0.0`                 | Endereço de escuta                 |
| `PG_PORT`              | `5450`                    | Porta PostgreSQL                   |
| `DYNAMO_ENDPOINT`      | `http://cs-dynamo-db:8000`| Endpoint DynamoDB                  |
| `DYNAMO_REGION`        | `us-east-1`               | Região AWS (qualquer para local)   |
| `DYNAMO_TABLE_AUDIT`   | `cs-audit-log`            | Tabela de audit log                |
| `DYNAMO_TABLE_APPLOGS` | `cs-app-logs`             | Tabela de application logs         |
| `AWS_ACCESS_KEY_ID`    | `AKIAIOSFODNN7EXAMPLE`    | Access key (qualquer para local)   |
| `AWS_SECRET_ACCESS_KEY`| `wJalrXUtnFEMI/...`       | Secret key (qualquer para local)   |
