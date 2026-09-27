# FlashEats: dependable late-delivery evidence (Track A)

FlashEats wants to understand where delivery time accumulates and which recorded operational conditions or interventions are associated with late orders. This project reconstructs an **order-grain** workflow and produces five descriptive analyses from a reproducible pipeline. The working Late Delivery Rate (LDR) is **not stakeholder-approved**. Operations, support, finance and the historical dashboard use competing interpretations; see [ARCHITECTURE.md](ARCHITECTURE.md#4-late-delivery-rate-contract).

**Users and stakeholders.** Operations and dispatch leads can use the provisional lateness baseline and observed lifecycle comparisons to decide which pre-pickup processes to investigate and whether to instrument driver arrival at restaurants. Restaurant managers can inspect the same order-level evidence when reviewing handoff timing; support leads can compare the separately labelled >10-minute view when discussing service policy. Finance and the eventual KPI owner must decide cancellation/refund treatment and approve a denominator before this becomes an official performance measure. Traffic and intervention comparisons help prioritize follow-up questions; they do **not** show that an intervention caused an outcome or that changing traffic conditions would change delivery performance.

## Sources and retrieval

The 12 supplied files in `data/raw/` are preserved: one SQLite database (orders, customers, drivers and restaurants), eight CSV exports, and three JSON files. SQL read-only connections retrieve SQLite tables; pandas/JSON readers retrieve files. Separately, the submission-owned local Dispatch API provides **one record per order** through `GET /dispatch/orders` pages. Its responses are not the nested `driver_events.json` telemetry: that file has one document per driver and 10,035 nested events. The pipeline checks API page sequence, reported totals and order-ID uniqueness, and retains raw pages by run date. The source-owner identities are unverified. The complete source map and [Mermaid workflow diagram](ARCHITECTURE.md#5-workflow-data-model) document grain, keys, gaps and relationships.

## Fresh-clone setup and one-command run

From this repository root, with Python 3 and its `venv` module available:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m pipeline.main --run-date 2026-09-27 --start-mock
```

The final command starts the included mock API on `127.0.0.1:8000`, waits for `/health`, retrieves all eight `/dispatch/orders` pages, and always stops its child service. It refuses to use an occupied port, so stop an unrelated service or set `DISPATCH_API_URL=http://127.0.0.1:<free-port>` first. Use a logical `YYYY-MM-DD` date for your own partition. `.env.example` lists optional environment overrides; the code reads exported variables, not `.env` automatically.

For separate development sessions, run `.venv/bin/python -m mock_api.server` in one terminal and the pipeline command **without** `--start-mock` in another. `GET /health` and `GET /dispatch/orders?page=1&page_size=200` are the relevant endpoints. The fixture is classroom-provided and unchanged; [provenance and the redistribution-permission caveat](mock_api/PROVENANCE.md) apply. No sibling `../reference/` checkout is needed.

## Pipeline and quality judgment

`pipeline.main` runs **Extract → Validate → Clean → Transform → Save** using the tested modules under `src/pipeline/`. A FAIL quality gate exits nonzero and publishes no new analytical model or metrics; its available validation evidence goes under `data/processed/diagnostics/run_date=.../attempt-.../`. WARN permits publication with quantified limitations. The actual snapshot is WARN: 1,603 SQLite order rows become 1,600 unique orders. Three duplicate order-ID pairs differ *only* in `traffic_bucket`; their canonical rows retain both source-row references, set traffic to null, and record disputed values in `cleaning_report.json`. A conflict in any other order field quarantines all rows for that ID and closes the gate. Six conflicting customer-interaction rows are also quarantined; no whole orders are removed.

Every one-to-many child source is aggregated to one row per `order_id` before a left join. SQLite dimensions are the primary reference sources; `restaurants.csv` is not joined as another dimension. The published model retains a customer key for linkage but excludes customer names, email addresses and free-text support fields. Missing milestones stay null, negative durations are flagged rather than repaired, and naive timestamps receive no invented timezone.

## Measured analyses from the supplied snapshot

The two lateness rates use the **same unapproved denominator**: delivered orders with both comparable promised ETA and actual delivery time. Thus 37 delivered orders without an actual time and 68 cancelled orders are excluded. Refund treatment and KPI ownership remain unresolved. Metrics are calculated from order timestamps/status, never from unverified `order_outcomes.csv` flags.

| Analysis | Observed evidence |
|---|---|
| Candidate operational LDR: actual delivery strictly after promised ETA | **843 / 1,495 = 56.39%**. |
| Support lateness variant: strictly more than 10 minutes after ETA | **349 / 1,495 = 23.34%**; denominator is not support-approved. |
| Observed lifecycle time | Creation→pickup: **27.69 min mean / 1,600 eligible**. Pickup→delivery: **45.79 min mean / 1,490 eligible**, excluding 105 missing delivery times and 5 invalid chronologies. In the LDR-eligible groups, late vs on-time/early means are 33.79 vs 19.77 minutes pre-pickup and 47.66 vs 43.36 minutes post-pickup. |
| Recorded intervention association | Late with intervention: **228/404 = 56.44%**; none recorded: **615/1,091 = 56.37%**. The total groups are 430 and 1,170 orders. This is association, not intervention effect. |
| Traffic segmentation | Low **178/360 = 49.44%**; medium **301/586 = 51.37%**; high **282/424 = 66.51%**; severe **81/122 = 66.39%**; unresolved/unknown **1/3**. Raw `HIGH` is grouped with `high` only for analysis; the three disputed nulls are not fabricated as a category. |

The larger *observed* late-versus-on-time segment gap is creation→pickup, but no restaurant-arrival event exists, so this cannot isolate driver approach from restaurant wait. No causal or statistical-significance claim is made. These outputs support prioritizing instrumentation and investigating segments/conditions, not a claim of measured business impact.

## Known / Unknown / Assumption / Limitation

- **Known:** 1,600 canonical orders, eight complete Dispatch pages, 843 late among 1,495 eligible delivered orders, and three unresolved traffic classifications.
- **Unknown:** formal KPI owner/approval, source-system owners and freshness contracts, refund treatment, and the derivation authority of external outcome labels.
- **Assumption:** the candidate LDR combines the VP Operations >0-minute threshold with a delivered-and-timestamp-complete denominator; it is explicitly provisional.
- **Limitation:** no `driver_arrived_at_restaurant` milestone; five observed pickup→delivery chronologies are invalid; interventions and traffic comparisons are descriptive, not causal.

## Outputs, tests and reruns

The run prints its date, gate, row count, two headline rates and paths. It writes:

```text
data/raw/dispatch/run_date=2026-09-27/page_1.json ... page_8.json
data/processed/run_date=2026-09-27/
  order_journey.jsonl      # UTF-8 JSON Lines, one complete object per order
  quality_report.json      # profiles and PASS/WARN/FAIL rule evidence
  cleaning_report.json     # actions, lineage and bounded quarantine summaries
  metrics.json             # five definitions, denominators, exclusions and results
  run_manifest.json        # source rows, API pages/records, gate and file inventory
```

Timestamp fields in JSON Lines are ISO-8601 strings; missing values are JSON null; list/dictionary lineage and count fields remain structured. Generated partitions, raw API pages, logs and caches are Git-ignored. Staged analytical files are checked before the run-date partition is replaced. A failed run does not publish a partial model or remove a prior valid partition. Repeating a date replaces, rather than appends to, its partition and its raw API snapshot. Logs are in `logs/run_date=YYYY-MM-DD.log`.

Run all tests with `PYTHONPATH=src .venv/bin/python -m pytest -q`. The configured Git remote is <https://github.com/Yuji25/FDE-Data-Foundations>; verify that the final commit is pushed and accessible before submitting its URL. See [SUBMISSION_EVIDENCE.md](SUBMISSION_EVIDENCE.md) and [DEMO.md](DEMO.md) for evidence and presentation guidance.
