# Extraction development instructions

## Environment

Use the existing repository virtual environment (`.venv/`). From the repository root, install dependencies there if needed:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
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
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_extract.py
```

The integration test starts the local mock service on a temporary port. API pages are written to a pytest temporary directory; tests do not modify the 12 original files in `data/raw/`.

## Phase 3 quality interface and tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_quality.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

`validate_sources(sources, dispatch)` returns `(copied_sources, quality_report, can_continue)`. The JSON-safe report has `profiles`, `rules`, `gate`, and `can_continue`. Each rule includes `rule_id`, `source`, `severity`, `status`, `observed_count`, bounded `examples`, and `explanation`. `clean_sources(sources, dispatch)` returns `(cleaned_sources, action_report)`; `action_report` has original, cleaned and quarantined counts, while full quarantined rows remain in `cleaned_sources["_quarantine"]`. When `can_continue` is false, do not publish an order-level model.

## Phase 3.1 order conflict evidence

The three raw duplicate order pairs differ only in `traffic_bucket`; see `ARCHITECTURE.md` for every disputed value and SQLite rowid. `validate_sources` reports `UNRESOLVED_TRAFFIC_BUCKET` as WARN and `CONFLICTING_ORDER_IDS` as FAIL only when another original order column differs. `clean_sources` returns 1,600 canonical orders on the supplied inputs. Its JSON-safe `action_report["sources"]["orders"]` includes `reconciled_order_ids`, `unresolved_traffic_classifications`, and a `reconcile_noncritical_traffic_conflict` action with each ID's zero-based `source_rows`, `differing_columns`, and original `traffic_values`. The cleaned row has null `traffic_bucket` and both references in `_source_rows`. Treat that null as unknown. Any critical duplicate conflict is quarantined and closes the gate; the unapproved KPI and external outcome-label authority remain unresolved.

## Phase 4 in-memory model and metrics

From the repository root, run the focused and complete suites with the existing environment:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_transform_metrics.py
PYTHONPATH=src .venv/bin/python -m pytest -q
```

`build_order_journey(cleaned_sources, cleaning_report) -> pd.DataFrame` requires the report returned by `clean_sources`; a FAIL or missing gate raises `ModelContractError`. It checks one row per canonical order, reduces each child to one row per `order_id`, verifies uniqueness before each left join, and preserves source-row order lineage. `calculate_metrics(order_journey) -> dict` returns five JSON-serializable descriptive analyses: unapproved candidate LDR, the >10-minute variant, observed lifecycle durations, intervention association, and traffic segmentation. Neither function writes outputs or invokes orchestration. The three unresolved traffic values remain null in the model and appear only in an `unknown/missing` analysis group.
