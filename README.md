# Northstar Services HR Assistant

MCP leave-management server, policy RAG and a Georgian CLI assistant for the
fictional company "Northstar Services".

> Work in progress. The full README (installation, configuration, usage, examples) is written in
> the documentation phase. See [docs/SPECIFICATION.md](docs/SPECIFICATION.md) and
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start (current state)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -e ".[dev]"
copy .env.example .env          # then fill in DATABASE_URL etc.
python -m scripts.migrate       # create the schema in Supabase
python -m pytest
```
