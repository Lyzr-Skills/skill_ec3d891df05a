---
name: opex-variance-analysis
description: Deterministic variance analysis for Elecar opex. Turns raw Anaplan query results (actual vs baseline) into the `data` object of the FP&A response contract v1.0, plus pre-formatted facts for the narrative. Use after every round of anaplan-opex-query fetches; never compute totals, variances or percentages yourself.
---

# Opex variance analysis

Runs `analyse_variance.py` (Python 3.9+, standard library only). Input is one JSON
document; output is `{"data": {...}, "facts": {...}}`.

- `data` is the contract's `data` object: copy it into the reply **unchanged**.
- `facts` holds formatted figures (`total_variance_fmt`, `by_region`,
  `top_drivers_fmt`, `top_drivers_share_of_variance_pct`, …) for `answer` and
  `key_points`. Quote these; never re-calculate or re-round.

## How to run

Write the input to `input.json` and run:

```
python3 analyse_variance.py input.json
```

(or pipe the JSON to stdin).

## Input

```json
{
  "fiscal_period": "FY2026 Q2",
  "currency": "USD",
  "periods": ["Apr 26", "May 26", "Jun 26"],
  "filters": {"region": "EMEA", "department": null, "account": null,
              "cost_center": null, "period_from": "2026-04", "period_to": "2026-06"},
  "queries": [
    {"level": "B", "role": "actual", "period": "Apr 26",
     "entity": "Elecar EURO", "region": "EMEA", "region_name": "EMEA",
     "rows": [ ...the "results" array from sql_query, untouched... ]},
    {"level": "B", "role": "plan", "period": "Apr 26",
     "entity": "Elecar EURO", "region": "EMEA", "region_name": "EMEA",
     "rows": [ ...results of the same query for Apr 25... ]}
  ]
}
```

One entry in `queries` per `sql_query` call:

| Field | Meaning |
|---|---|
| `level` | `A` regions (rows have `e3 entity`), `B` departments (rows have `d3 department`), `C` accounts (rows have `account is hierarchy`) |
| `role` | `actual` for the current period, `plan` for the baseline |
| `period` | The **actual** month this pairs with. For a baseline query on `Apr 25`, still write `Apr 26`. |
| `entity` | The `e3 entity` value you sliced to (levels B and C) |
| `region`, `region_name` | Label to group under, e.g. `EMEA` (optional; both EURO and Northern Europe queries can share `EMEA` and are summed) |
| `department` | The `d3 department` value you sliced to (level C only) |
| `rows` | The `results` array exactly as returned. Do not edit, round, filter or de-duplicate it. |

The script handles Chimera's duplicate rows, sums several entities that share a
region label, keeps only operating-expense accounts at level C, and pairs actual
and baseline rows.

## What it computes (contract v1.0 rules)

- `totals` from level A rows if present, else level B, else level C.
- `top_drivers`: up to 3 lines from the most detailed level present, in the
  direction of the total variance, largest first, with `pattern`
  (`single_period`, `one_off` = one month ≥ 80% of the line's variance,
  `run_rate` = every month ≥ 8% off in the same direction, else `mixed`) and
  `peak_period`.
- `histogram`: variance % of every monthly row at level B (else A), in the
  contract's 9 bins.
- `outliers`: up to 5 monthly rows at the most detailed level that are ≥ 15% off
  (or have no baseline), largest absolute variance first.
- Variance = actual − plan, so positive = overspend. Percentages have one decimal
  and are already × 100.

## If it fails

Put the error text in `caveats`, set `status` to `error`, and do not attempt the
analysis yourself.
