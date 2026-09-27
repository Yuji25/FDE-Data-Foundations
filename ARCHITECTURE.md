# FlashEats - Architecture & Pipeline Document

## 1. Business Goal & Context
- **Business Goal:** Reduce Late Delivery Rate (LDR).
- **Factual Limitation:** The current system has a "Ghost Mile" telemetry gap. There is no `driver_arrived_at_restaurant` event being logged in the driver telemetry. This makes it impossible to cleanly separate kitchen preparation time from driver transit time. We cannot build AI models predicting operational delays when the underlying system fails to instrument the necessary milestones.

## 2. Source Map & Data Grains
The data landscape involves multiple sources of different types (SQLite, CSVs, APIs).
- **Orders:** SQLite database. Grain: 1 row = 1 unique customer order.
- **Customers:** CSV. Grain: 1 row = 1 customer.
- **Restaurants:** CSV. Grain: 1 row = 1 restaurant.
- **Tickets:** CSV. Grain: 1 row = 1 support ticket (1:N relationship to orders).
- **Actions/Interventions:** CSV. Grain: 1 row = 1 operational intervention/action (1:N relationship to orders).
- **Dispatch API:** REST API returning nested JSON for driver telemetry/events. Grain: 1 row = 1 driver event (1:N relationship to orders).

## 3. Workflow Data Model
To build a dependable analytical layer, we organize around the real-world operational order lifecycle into a unified `order_journey` model.
- **Fan-Out Prevention:** Since datasets like Tickets, Actions, and Dispatch API events have a 1:N (one-to-many) relationship with Orders, joining them directly will cause a fan-out, duplicating order rows and corrupting operational/financial metrics.
- **Aggregation Strategy:** All child tables (1:N) will be aggregated to a `1 row = 1 order` grain (e.g., summarizing total tickets or extracting the first ETA_VIEWED timestamp) before performing a Left Join onto the primary Orders table.

## 4. Pipeline Stages & Idempotency
The pipeline follows standard FDE stages to ensure dependability over simple repeatability.
- **Stages:**
  1. **Extract:** Multi-source data ingestion (SQLite, CSVs, APIs with bounded exponential backoff and pagination).
  2. **Validate:** Automated validation gates (PASS, WARN, FAIL) profiling data against structural and business lifecycle contracts.
  3. **Clean:** Normalizing data, removing duplicates, coercing types, and handling missing timestamps.
  4. **Transform:** Aggregating 1:N data to the correct grain and left-joining to create the comprehensive `order_journey`.
  5. **Save:** Storing the processed outputs across storage layers (Bronze for raw, Silver for cleaned/order_journey, Gold for metrics).

- **Idempotency Strategy:**
  To guarantee that running the pipeline multiple times safely produces the same result without data corruption:
  - We partition data by logical run date.
  - We replace partitions cleanly on reruns.
  - We use atomic file swaps (e.g., `os.replace` via temporary files) to prevent partial write corruption in the event of mid-save failure.
