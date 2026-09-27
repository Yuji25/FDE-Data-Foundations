# FlashEats 3–5-minute demo

Use the repository root and a prepared `.venv` (`README.md` has fresh-clone setup). Do not claim a stakeholder-approved KPI, causal intervention effect, or observed restaurant-arrival timestamp.

1. **Problem and decision (0:00–0:35).** Open `README.md`. “FlashEats needs a dependable baseline for late delivery and a way to locate observable delay, not a prediction model built on unverified labels. This is Track A, at one row per order.” Show that the candidate LDR is explicitly unapproved.
2. **Sources and workflow (0:35–1:15).** Open `ARCHITECTURE.md` sections 2, 3 and the Mermaid diagram in section 5. Point out read-only SQLite orders/dimensions, CSV exports, nested local driver telemetry and the *separate* paginated Dispatch API. Mention eight complete pages, 1,600 Dispatch records, and the missing `driver_arrived_at_restaurant` milestone.
3. **FDE judgment call (1:15–1:55).** Open section 8 of `ARCHITECTURE.md` and `src/pipeline/clean.py`. The three duplicate order pairs disagree only on traffic. Show that each becomes one canonical order with null traffic and both disputed values/source rows in cleaning evidence; a critical-field conflict instead FAILs the gate. This preserves the 1,600-order population without inventing a traffic value.
4. **Run the pipeline (1:55–2:35).** Execute:

   ```bash
   PYTHONPATH=src .venv/bin/python -m pipeline.main --run-date 2026-09-27 --start-mock
   ```

   Show extraction logs, the page-3 HTTP 500 and page-5 HTTP 429 retries, WARN gate, 1,600 model rows and output paths. The command starts and stops only its own mock server. A same-date rerun replaces the partition.
5. **Model and five analyses (2:35–4:15).** Inspect `data/processed/run_date=2026-09-27/run_manifest.json`, `quality_report.json`, `cleaning_report.json` and `metrics.json`. Show `order_journey.jsonl` is one JSON object per order, not a multiplied event join. Report candidate LDR **843/1,495 = 56.39%**, support >10-minute **349/1,495 = 23.34%**, creation→pickup **27.69 min** and pickup→delivery **45.79 min** means, intervention groups **228/404** vs **615/1,091** late, and traffic groups from `README.md` (high **282/424**, low **178/360**, plus three unknown traffic values). These are descriptive associations.
6. **Limits and handoff (4:15–4:45).** Point to `README.md` Known/Unknown/Assumption/Limitation: KPI owner/denominator/refund policy unapproved, external outcome-file authority unverified, and no restaurant-arrival milestone. The result supports better instrumentation and investigation, not a proven business impact. Close with `SUBMISSION_EVIDENCE.md` and the configured Git remote; confirm the final commit is pushed before submitting.
