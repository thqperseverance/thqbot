# Session Storage Strategy Guide

ithqbot uses a flexible strategy pattern to manage conversation memory and sessions. This allows the system to seamlessly transition from local development to a high-availability, multi-tenant production environment.

## Overview

The `SessionManager` uses a factory pattern to instantiate an underlying storage engine that complies with the `BaseSessionStore` interface.

Enterprise defaults use PostgreSQL for memory and cron, while sessions can remain local `.jsonl` or move to Redis/PostgreSQL via store URIs in `config.json`.

For Skill Graph runs, the current implementation reuses the agent storage configuration with a stricter rule:
- `GraphExecutor` prefers `agents.defaults.sessionStoreUri`
- If the session store is not PostgreSQL, it falls back to `agents.defaults.memoryStoreUri`
- Only PostgreSQL-backed URIs enable persistent graph recovery
- If neither URI points to PostgreSQL, graph run state falls back to in-memory storage and cannot survive process restarts

## Configuration

You can configure storage strategy in `~/.ithqbot/config.json` under:
- `agents.defaults.sessionStoreUri`
- `agents.defaults.memoryStoreUri`
- `agents.defaults.cronStoreUri`

| Strategy | Connection URI Example | Description |
| :--- | :--- | :--- |
| **File (Session only)** | `file:///path/to/sessions` | Fallbacks to `workspace/sessions` if no session URI is provided. Memory/Cron are not file-backed in enterprise mode. |
| **Redis** | `redis://:password@10.0.0.1:6379/0` | High performance, memory-based. **Must enable Redis AOF persistence** if used as the sole source of truth. Best for multi-instance deployments (Docker/K8s). |
| **PostgreSQL** | `postgresql://user:pass@host:5432/ithqbot` | Durable relational storage. Supports `postgresql+psycopg://...` variants. |

### Example Usage (`config.json`)

```json
{
  "agents": {
    "defaults": {
      "sessionStoreUri": "postgresql://postgres:postgres@127.0.0.1:5432/ithqbot",
      "memoryStoreUri": "postgresql://postgres:postgres@127.0.0.1:5432/ithqbot",
      "cronStoreUri": "postgresql://postgres:postgres@127.0.0.1:5432/ithqbot",
      "requireExternalSessionStore": true,
      "requireExternalMemoryStore": true,
      "requireExternalCronStore": true
    }
  }
}
```

Or for local session files only:

```json
{
  "agents": {
    "defaults": {
      "sessionStoreUri": "file:///tmp/my_ithqbot_sessions"
    }
  }
}
```

## PostgreSQL Storage Deep Dive

Current implementation supports enterprise-grade persistence for:
- Session state (`ithqbot_sessions`)
- Agent memory (`ithqbot_memory`, `ithqbot_memory_history`)
- Cron jobs and execution queue (`ithqbot_cron_jobs`, `ithqbot_cron_events`, `ithqbot_cron_leases`)
- Skill Graph runs (`graph_runs`, `graph_nodes`)

Cron in PostgreSQL mode follows Scheme B style decoupling:
- Scheduler side elects lease owner via `ithqbot_cron_leases`
- Due jobs enqueue idempotent events with `dedupe_key`
- Worker side claims events with row-level locking and retries/backoff
- Tenant/account dimensions are indexed via `tenant_id` and `account_id`

### Graph State Persistence

Graph execution persistence is currently PostgreSQL-only. This is intentional because graph recovery depends on:
- transactional updates for `graph_runs` and `graph_nodes`
- JSONB state snapshots for node input/output and global run state
- stable reload semantics during `resume()` after interaction interrupts

Recommended production setup:
- Use the same PostgreSQL cluster for `sessionStoreUri`, `memoryStoreUri`, `cronStoreUri`, and graph persistence unless you have a clear isolation requirement
- Enable regular backup/restore procedures for the database that stores graph state
- Avoid mixing file session storage with graph recovery expectations in multi-instance deployments
- Keep application instances on the same graph definition set under `graphs/`, otherwise persisted `graph_id` may not be reloadable on another instance

Current constraint:
- Redis can back session and memory layers, but it is not yet a supported durable backend for `ithqbot.graph.state_manager.StateManager`
- If you deploy sessions on Redis without a PostgreSQL `sessionStoreUri` or `memoryStoreUri`, graph runs will execute with in-memory state only

## Redis Storage Deep Dive

The `RedisSessionStore` provides several key features out of the box:

1. **Multi-Tenant Isolation**: Keys are stored using the pattern `ithqbot:session:{tenant_id}:{session_key}`. The `tenant_id` is automatically extracted from the global `ithqbot.context`. If no tenant is present, it defaults to `default`.
2. **Performance**: Uses `scan_iter` for non-blocking list queries.
3. **Persistence Warning**: If you use Redis, ensure your Redis server has AOF (Append Only File) persistence enabled, otherwise, restarting the Redis container will result in complete "Agent Amnesia" (loss of all conversation history).

### Redis and Graph Workloads

Redis is suitable today for:
- session storage
- memory storage
- observability/event-style read models

Redis is not the current source of truth for graph run recovery. For workflows that require:
- resumable multi-step execution
- node-level failure diagnosis
- restart-safe waiting states

you should still provision PostgreSQL for graph persistence, even if Redis is enabled elsewhere in the stack.

### Setting up Redis via Docker

Here is a practical example of setting up a local Redis instance using Docker:

```bash
(.venv) winlmp@ubuntuS:~/tools/redis-docker$ docker compose up -d 
 [+] up 13/13 
  ✔ Image redis:7.2              Pulled                                                                         209.7s 
  ✔ Network redis-docker_default Created                                                                          0.1s 
  ✔ Container my-redis           Created                                                                          0.6s 
 (.venv) winlmp@ubuntuS:~/tools/redis-docker$ 
```

*Ensure your `docker-compose.yml` mounts a volume and configures safe persistence like this snippet:*
```yaml
services:
  redis:
    image: redis:7.2
    command: redis-server --appendonly yes --appendfsync everysec
    volumes:
      - ./data:/data
    ports:
      - "6379:6379"
```

## Implementing a Custom Store

If you need to implement a new storage backend (e.g., MongoDB), follow these steps:

1. Create a new class extending `BaseSessionStore` in `ithqbot/session/`.
2. Implement the 4 core abstract methods:
   - `load(self, key: str) -> Optional[Session]`
   - `save(self, session: Session) -> None`
   - `delete(self, key: str) -> None`
   - `list_sessions(self) -> List[Dict[str, Any]]`
3. Register your new prefix scheme in `ithqbot/session/factory.py` inside the `create_session_store` function.
