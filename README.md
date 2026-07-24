# ES.ConexaoSolidaria.DynamoPgProxy

Serviço utilitário de observabilidade da plataforma **Conexão Solidária**, desenvolvido para a ONG Esperança Solidária como parte do Hackathon POSTECH/FIAP.

Esse serviço é utilizado **apenas no deploy Local**. No Cloud (AWS), a ponte entre o Grafana e o DynamoDB é feita por uma **Lambda**. Mais detalhes podem ser verificados no repositório de infraestrutura — [https://github.com/gmerendi/ES.ConexaoSolidaria.Infra].

Um serviço Python que implementa o **protocolo wire do PostgreSQL** e traduz queries SQL diretamente para o **DynamoDB** — sem duplicação de dados, sem camadas extras, sem plugins no Grafana. O Grafana conecta nele como se fosse um Postgres de verdade, usando o datasource nativo.  O datasource do Grafana só é disponivel na versão Enterprise (paga).

```
Grafana (datasource PostgreSQL nativo)
    ↓ SQL
cs-dynamo-pg-proxy:5450
    ↓ DynamoDB Scan com FilterExpression
DynamoDB Local (cs-audit-log / cs-app-logs)
```

---

## Sumário
- [Arquitetura](#arquitetura)
- [Stack Tecnológica](#stack-tecnológica)
- [Tabelas Disponíveis](#tabelas-disponíveis)
- [Queries SQL Suportadas](#queries-sql-suportadas)
- [Como Rodar Localmente](#como-rodar-localmente)
- [Integrar à Infraestrutura](#integrar-à-infraestrutura)
- [Variáveis de Ambiente](#variáveis-de-ambiente)
- [Dashboards](#dashboards)
- [Testes](#testes)
- [Estrutura do Projeto](#estrutura-do-projeto)
- [Github Actions](#github-actions)

---

## Arquitetura

O DynamoDB não tem um driver PostgreSQL nem suporte nativo a datasources do Grafana — a alternativa mais simples é fazer o próprio Grafana falar com um serviço que finge ser um Postgres. Este proxy escuta na porta `5450`, implementa o suficiente do protocolo wire do PostgreSQL para o Grafana se conectar e autenticar, faz um parsing simplificado do SQL recebido e o traduz para uma operação `Scan` (com `FilterExpression`, quando aplicável) contra as tabelas do DynamoDB.

> Este é um serviço específico do ambiente **local/Kubernetes**. Em produção (AWS), essa integração é substituída por uma função **Lambda** que conecta o Grafana ao DynamoDB — o diagrama completo está no repositório de infraestrutura.

## Stack Tecnológica

- **Python 3.12** (`asyncio`, sockets — implementação própria do protocolo wire do PostgreSQL)
- **boto3** — cliente do DynamoDB
- **Docker** / **Docker Compose**

## Tabelas Disponíveis

| Tabela SQL | Tabela DynamoDB | Descrição |
|---|---|---|
| `audit_log` | `cs-audit-log` | Audit trail do sistema |
| `app_logs` | `cs-app-logs` | Logs de aplicação/trace |

### Colunas — `audit_log`

| Coluna | Campo DynamoDB | Tipo |
|---|---|---|
| `timestamp` | `SK` | text |
| `service` | `ServiceName` | text |
| `operation` | `Operation` | text |
| `changed_by` | `ChangedBy` | text |
| `resource_id` | `ResourceId` | text |
| `ip_address` | `IpAddress` | text |
| `pk` | `PK` | text |
| `payload` | `Payload` | text |

### Colunas — `app_logs`

| Coluna | Campo DynamoDB | Tipo |
|---|---|---|
| `timestamp` | `Timestamp` | text |
| `level` | `LogLevel` | text |
| `caller` | `Caller` | text |
| `message` | `Message` | text |
| `correlation_id` | `CorrelationId` | text |
| `data` | `Data` | text |
| `type` | `Type` | int |

## Queries SQL Suportadas

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

-- Tabelas disponíveis
SELECT table_name FROM information_schema.tables
```

## Como Rodar Localmente

### Pré-requisitos
- Docker Desktop 4.79.0
- Um DynamoDB Local acessível (provisionado pelo `ES.ConexaoSolidaria.Usuarios` ou `ES.ConexaoSolidaria.Campanhas`)

### Rodando standalone

```bash
git clone https://github.com/gmerendi/ES.ConexaoSolidaria.DynamoPgProxy.git
cd ES.ConexaoSolidaria.DynamoPgProxy

# Ajuste as variáveis de ambiente se necessário
docker compose up --build
```

O proxy fica disponível em `localhost:5450`.

## Integrar à Infraestrutura

Para usar o proxy junto do restante da stack (rede `csnetwork`, DynamoDB Local dos outros serviços e provisionamento do Grafana), adicione ao `docker-compose.yml` da infraestrutura:

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

E o datasource no provisionamento do Grafana (`grafana_datasource`):

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

## Variáveis de Ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `PG_HOST` | `0.0.0.0` | Endereço de escuta |
| `PG_PORT` | `5450` | Porta PostgreSQL (wire protocol) |
| `DYNAMO_ENDPOINT` | `http://cs-dynamo-db:8000` | Endpoint do DynamoDB |
| `DYNAMO_REGION` | `us-east-1` | Região AWS (qualquer valor serve localmente) |
| `DYNAMO_TABLE_AUDIT` | `cs-audit-log` | Tabela de audit log |
| `DYNAMO_TABLE_APPLOGS` | `cs-app-logs` | Tabela de application logs |
| `AWS_ACCESS_KEY_ID` | `AKIAIOSFODNN7EXAMPLE` | Access key (qualquer valor serve localmente) |
| `AWS_SECRET_ACCESS_KEY` | `wJalrXUtnFEMI/...` | Secret key (qualquer valor serve localmente) |

> **Atenção:** as credenciais AWS acima são dummy — o DynamoDB Local não valida credenciais reais. Nunca reutilize esses valores em produção.

## Dashboards

Os dashboards prontos deste repositório usam o datasource `DynamoDB-PG` e devem ser copiados para `observability/grafana/provisioning/dashboards/` no repositório de infraestrutura:

- `cs-audit-log.json` — Audit Log, com filtros de serviço e operação
- `cs-app-logs.json` — Application Logs, com filtro por Correlation ID (trace completo de uma requisição)

## Testes

### 1. Instalar as dependências (app + testes)
```bash
pip install -r src/requirements.txt
pip install -r tests/requirements-test.txt
pip install pytest-cov   # só se quiser o relatório de cobertura
```

### 2. Rodar os testes
```bash
pytest tests/test_pg_dynamo_proxy.py -v --cov=src --cov-report=term
```

## Estrutura do Projeto

```
ES.ConexaoSolidaria.DynamoPgProxy/
├── pg_dynamo_proxy.py       # Implementação do protocolo wire do PostgreSQL + tradução SQL → DynamoDB Scan
├── requirements.txt          # boto3
├── Dockerfile
├── docker-compose.yml         # Execução standalone do proxy
├── cs-audit-log.json          # Dashboard Grafana — Audit Log
└── cs-app-logs.json           # Dashboard Grafana — Application Logs
```

Projeto desenvolvido para o Hackathon **POSTECH** — grupo 1.

## Github Actions

Esse repositorio não possui Github Action por ser utilizado somente em desenvolvimento local para servir de datasource entre Grafana e DynamoDb.
