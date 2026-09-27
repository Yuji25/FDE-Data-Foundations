# Extraction development instructions

## Environment

From the repository root, create/activate a virtual environment and install:

```bash
python3 -m pip install -r requirements-dev.txt
```

`.env.example` documents configuration values, but the project does not load `.env` automatically. Export overrides in the shell when needed.

## Start the submission-owned mock Dispatch API

```bash
python3 -m mock_api.server
```

The server listens on `http://127.0.0.1:8000` by default and exposes:

- `GET /health`
- `GET /dispatch/orders?page=1&page_size=200`
- `GET /dispatch/orders/<order_id>`

The default behavior reproduces the classroom service's one-time HTTP 500 on page 3 and HTTP 429 on page 5. Use `--no-transient-failures` only for manual smoke testing that does not exercise retries.

The fixture provenance and redistribution caveat are recorded in `mock_api/PROVENANCE.md`.

## Run focused extraction tests

```bash
PYTHONPATH=src pytest -q tests/test_extract.py
```

The integration test starts the local mock service on a temporary port. API pages are written to a pytest temporary directory; tests do not modify the 12 original files in `data/raw/`.
