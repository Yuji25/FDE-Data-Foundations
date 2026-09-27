# FlashEats Classroom Challenge Answers

All values below were executed locally against `data/raw/` and the WARN-gated canonical 1,600-order model published for `run_date=2026-09-27`. “Late” is provisional: delivered, comparable promised/actual timestamps, and actual strictly after promised. External `order_outcomes` flags are not treated as truth.

## Class 5

### 1. Size of the late-delivery problem

**843/1,495 = 56.39%** eligible deliveries were late; median lateness among late orders was **8.03 min**. Worst five: O00104 87.35, O00102 86.29, O00101 85.51, O00103 69.29, O01081 50.27 minutes. Excluded: 68 cancellations and 37 delivered orders missing actual time. Raw 1,603 rows contain three traffic-only duplicate conflicts; documented reconciliation yields 1,600 orders and unknown traffic for those IDs.

### 2. “Traffic is the problem”

| Dimension | Comparison | Late rates |
|---|---|---:|
| Traffic | low vs high | 49.44% vs 66.51% |
| Weather | clear vs heavy rain | 53.27% vs 73.61% |
| Distance | ≤5 km vs >15 km | 53.62% vs 57.96% |

Traffic/weather are associated with delay, but this is not causal; preparation, dispatch selection, geography and missing arrival instrumentation may confound the result.

### 3. Customer Support

Normalized complaint counts: late delivery 40, ETA changed 37, restaurant delay 37, ready-but-waiting 33, status mismatch 29, driver-not-moving 25, ETA issue 1. There are 202 rows, one duplicate ticket ID and three missing order IDs. Ticket-linked eligible deliveries are **163/197 = 82.74% late** (median 11.43 min) versus **680/1,298 = 52.39%** (0.50 min). Tickets reveal expectation/status/movement problems timestamps cannot, but are self-selected.

### 4. Dispatch API completeness

Eight sequential 200-record pages produced 1,600 records and unique IDs, exactly matching the reported total and canonical ID set. One HTTP 500 (page 3) and one 429 (page 5) were retried. All successful pages are preserved in `data/raw/dispatch/run_date=2026-09-27/`.

### 5. Delay attribution

Telemetry has 10,035 events: 1,600 assigned, 5,371 GPS pings, 1,532 pickup and 1,532 delivery, but **no arrival-at-restaurant event**. Assignment, pickup and delivery are observed when present; restaurant arrival is only a possible GPS inference without an approved geofence/accuracy policy. Reliable attribution is therefore not possible.

### Class 5 final synthesis

The provisional problem is 56.39%. High/severe traffic, rain and ticketed journeys are associated with lateness, not proven causes. Trust order timestamps for the provisional outcome, Dispatch for assignment snapshot, tickets for complaints and telemetry for observed events. Resolve KPI ownership, timestamp completeness, disputed traffic and the arrival milestone before building an AI predictor.

## Class 6

### 1. Defend the 56% claim

Validation found 1,603 rows/1,600 IDs, 37 delivered orders missing completion, 4 promised-before-created rows, 5 pickup-after-delivery rows and no agreed KPI owner. IDs O00120, O00723 and O01302 have traffic-only conflicts; the canonical record retains both source rows and sets traffic unknown rather than taking the first value. Verdict: reproducible but not yet publishable as an official KPI.

### 2. Competing “late” definitions

| Definition | Result |
|---|---:|
| Delivered/comparable, >0 min | 843/1,495 = 56.39% |
| Delivered/comparable, >10 min | 349/1,495 = 23.34% |
| Any delay / all canonical orders | 843/1,600 = 52.69% |
| Historical delivered/non-null | 843/1,495 = 56.39% |

Publish only a labelled provisional 56.39% with exclusions; a formally named KPI owner must approve threshold, denominator and cancellation/refund handling.

### 3. Categories

Trim/lowercase normalization is safe for `Delivered`, `HIGH`, ready variants and late-delivery formatting variants. Do not merge `handoff`/`handed_off`, `unknown`/known statuses or `ETA issue`/`eta_changed` without owner confirmation.

### 4. Cross-source integrity

| Relationship | All-row coverage | Non-null coverage | Issue |
|---|---:|---:|---|
| order restaurant → restaurants | 99.81% | 100% | 3 null keys |
| order driver → drivers | 99.81% | 100% | 3 null keys |
| ticket order → orders | 98.51% | 100% | 3 missing keys |
| restaurant status order → orders | 100% | 100% | none |

Even 1% may be unacceptable for accountability, live action or resolving the exact affected cases.

### 5. Freshness

502 status rows cover 500/1,600 orders (31.25%); six updates predate creation and none follow observed delivery. Median update-to-delivery gap is 53.77 min (absolute p90 78.52). Weekly analytics is WARN with caveats; live ETA and restaurant accountability FAIL until coverage, semantics and an owner-defined freshness SLA improve.

### 6. Validation gate

Business grain WARN; chronology WARN; KPI definition UNKNOWN; category semantics WARN; mapping WARN; freshness FAIL. Do not publish bare “56%”; only a clearly labelled provisional metric is defensible before owner approval and remediation.

## Class 7

### 1. Order lifecycle

Executed timelines for O00003 (on time), O00001 (late) and O00781 (intervention) union and sort observed order, telemetry, app, support, restaurant and intervention events. Untimed outcomes are not invented as events.

### 2. Canonical project model

Customers use customer_id; canonical orders order_id plus customer/restaurant/driver FKs; app actions action_id; tickets ticket_id (one duplicate quarantined); interventions intervention_id; outcomes one row/order. Aggregating one-to-many sources before joining prevents fan-out and models the workflow rather than source storage.

### 3. Interaction → intervention → outcome

**163/843 late orders** had support tickets; **430 orders** received intervention. Most common: driver reassignment 155 (restaurant contact 116, priority dispatch 95, credit 64). Defining frustration as support or cancel attempt, **149** frustrated journeys had no intervention; **122** were late.

### 4. Business metrics

| Metric | Value |
|---|---:|
| Provisional LDR | 843/1,495 = 56.39% |
| Support-contact rate | 198/1,600 = 12.38% |
| Cancel-attempt rate | 10/1,600 = 0.63% |
| Intervention reach | 430/1,600 = 26.88% |
| Median creation→pickup | 26.14 min |

### 5. Workflow investigation

Support-linked vs unlinked late rates are 82.74% vs 52.39%; intervention vs none is 56.44% vs 56.37%. Priority dispatch has the lowest observed intervention-type late rate (44/86 = 51.16%). R050 contributes the most late orders (22/35). Forty-four journeys had support + intervention + late outcome. These are associations; selection/timing and case mix prevent causal claims.

### 6. KPI linkage

KPI → LDR/>10-minute outcomes → creation-to-pickup and customer-friction metrics → assignment/reassignment, priority, contact and credit interventions → order/events/telemetry/app/support/Dispatch sources. Operations controls interventions, not traffic/weather or the outcome. The missing driver-arrival event most limits attribution; instrument a governed geofence arrival plus intervention eligibility/decision timestamps next.

## Assumptions and limitations

- All source timestamps are interpreted as supplied; no timezone is invented.
- Missing milestones remain missing; absence of activity becomes zero only for present, validated sources.
- Refund treatment, source ownership and official KPI ownership remain unknown.
- Traffic, support and intervention comparisons are descriptive associations, not causal estimates.
