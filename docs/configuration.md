# Configuration

How to point puxti at your projects, your LLM provider and your storage.

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com). Only needed with the default provider. |
| `GITHUB_TOKEN` | Personal access token with `repo` scope (Contents + Pull requests write) |
| `DBT_PROJECT_DIR` | Path to your dbt project root (the directory containing `dbt_project.yml`) |
| `DBT_PROFILES_DIR` | Path to your dbt profiles directory — usually `~/.dbt` |
| `LLM_PROVIDER` | LLM provider (optional, default `anthropic`) — see [LLM providers](#llm-providers-byok) |
| `LLM_MODEL` | Model ID (required for non-Anthropic providers; overrides the default `claude-sonnet-5-5` otherwise) |
| `LLM_API_KEY` | API key for the selected provider (falls back to `ANTHROPIC_API_KEY` for the default provider) |
| `LLM_BASE_URL` | Override the provider's base URL (custom endpoints, non-default Bedrock regions) |
| `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK` | USD per million tokens for `--dry-run` cost estimates on models puxti doesn't know (optional) |
| `LLM_CONCURRENCY` | Max parallel LLM calls during `puxti scan` (optional, default 4) — raise only if your provider's rate-limit tier allows it |
| `DATABASE_URL` | Postgres DSN for the Knowledge Graph (optional) — see [Storage backends](#storage-backends). Unset uses the default local SQLite file. |

---

## Storage backends

Puxti's Knowledge Graph lives in a local SQLite file at `~/.puxti/graph.db` by default — no setup, nothing to run.

To store it in **Postgres** instead (for a shared or server deployment), install the extra and point `DATABASE_URL` at your database:

```bash
pip install 'puxti[postgres]'
export DATABASE_URL=postgresql://user:password@host:5432/puxti
```

Puxti creates its tables on first connect, so the database must already exist — create it (or reuse one) before running any command. Both backends store identical data, so every command (`scan`, `define`, `impact`, `describe`, …) and the MCP server work the same way against either.

---

## LLM providers (BYOK)

Puxti is provider-agnostic: bring a key from any of these and set two or three env vars. Every provider below speaks the OpenAI-compatible wire format at its own endpoint; Anthropic (the default) uses its native API.

```bash
# Example: Mistral
LLM_PROVIDER=mistral
LLM_MODEL=mistral-large-latest
LLM_API_KEY=...
```

| `LLM_PROVIDER` | Endpoint | Where to get a key |
|---|---|---|
| `anthropic` *(default)* | native Anthropic API | [console.anthropic.com](https://console.anthropic.com) — or just set `ANTHROPIC_API_KEY` |
| `openai` | api.openai.com | [platform.openai.com](https://platform.openai.com) |
| `mistral` | api.mistral.ai | [console.mistral.ai](https://console.mistral.ai) |
| `glm` | open.bigmodel.cn | [bigmodel.cn](https://open.bigmodel.cn) (Zhipu / GLM) |
| `deepseek` | api.deepseek.com | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | api.groq.com | [console.groq.com](https://console.groq.com) |
| `openrouter` | openrouter.ai | [openrouter.ai](https://openrouter.ai) — one key, many models |
| `gemini` | Gemini OpenAI-compat endpoint | [aistudio.google.com](https://aistudio.google.com) |
| `bedrock` | Bedrock Mantle (`us-east-1`; other regions via `LLM_BASE_URL`) | Bedrock API key from the AWS console |
| `bedrock-anthropic` | Claude on Bedrock Mantle, Anthropic Messages API (`us-east-1`; other regions via `LLM_BASE_URL`) | Bedrock API key from the AWS console; set `LLM_MODEL` to the Bedrock model ID |
| `ollama` | localhost:11434 — no key needed | local install from [ollama.com](https://ollama.com) |
| `custom` | anything OpenAI-compatible via `LLM_BASE_URL` (vLLM, proxies) | your infrastructure |

Notes:

- **Cost estimates** (`--dry-run`): exact token counts and dollar costs on Anthropic; approximate token counts elsewhere, with dollar costs only when you provide `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK` — puxti never guesses a price.
- **`bedrock-anthropic` token counts**: puxti tries the exact `count_tokens` call first and falls back to an approximate count if the endpoint does not implement it. Dollar costs need `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK`.
- **Quality**: the semantic prompts expect strict JSON output; frontier models handle this reliably, small local models may not.
- **Not covered**: Vertex AI (OAuth-based short-lived tokens, not a durable key). Claude on Bedrock works through `bedrock-anthropic` (Anthropic Messages API) or, for any model on Mantle, the OpenAI-compatible `bedrock` provider.

---

## Workspace config (`.puxti.yml`)

If your projects live in separate repos, add a `.puxti.yml` at the root of your workspace — the directory that contains the repo clones. Puxti discovers it by walking up from your current directory (git-style).

```yaml
version: 1

connectors:
  dbt:
    project_dir: ./dbt
    repo: your-org/your-dbt-repo
    base_branch: main
    # repo_subdir: analytics   # set when the project is not at the repo root

  airflow:
    project_dir: ./airflow
    repo: your-org/your-airflow-repo
    dags_dir: dags/
    base_branch: main

  # Application schema (Prisma ORM) — tables and fields become entities,
  # relations become lineage. schema_path defaults to prisma/schema.prisma.
  prisma:
    project_dir: ./app
    repo: your-org/your-app-repo
    schema_path: prisma/schema.prisma

  # Plain CREATE VIEW .sql files — views, their columns, and the tables they
  # read from become entities and lineage. dialect is any sqlglot dialect name.
  sql_views:
    project_dir: ./app
    repo: your-org/your-app-repo
    views_dir: db/views
    dialect: postgres
    default_schema: public
```

Every connector accepts `project_dir`, `repo`, `base_branch` (default `main`) and `repo_subdir`. Set `repo_subdir` when the project lives in a subdirectory of its repo, so the file paths in generated PRs are relative to the repo root. For `capture`, the `--repo-subdir` flag overrides it on the dbt connector.

With this in place:
- `--repo` is no longer required for `capture`, `redefine`, or `health`
- `--dbt-project-dir` resolves from `connectors.dbt.project_dir` automatically
- `puxti health` checks GitHub write access for each connector repo
- `puxti scan` scans every configured producer (dbt, prisma, sql_views) and
  links references across them — a view reading a Prisma-managed table gets a
  real lineage edge
- `puxti capture` propagates column renames through every configured connector
  and opens one PR per repo

Prisma rename diffs patch `schema.prisma` only — applying them still requires
`prisma migrate dev` and `prisma generate`, and every generated PR says so.

Run `puxti config` from anywhere inside the workspace to verify it was found:

```
.puxti.yml:  /your/workspace/.puxti.yml  found
  connectors: dbt (your-org/your-dbt-repo · ./dbt), airflow (your-org/your-airflow-repo · ./airflow)
```

Precedence: CLI flags > `.puxti.yml` > environment variables.

---

## Verify setup

```bash
puxti config   # show resolved config values and file locations
puxti health   # verify connectivity to all services
```

Expected output from `puxti health`:

```
✓ Knowledge Graph  (~/.puxti/graph.db)
✓ Anthropic API key
✓ dbt manifest
✓ GitHub write access — your-org/your-dbt-repo (dbt)
```
