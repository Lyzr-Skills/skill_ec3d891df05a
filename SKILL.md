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
  "filters": {"region": "NORTHERN-EUROPE", "department": null, "account": null,
              "cost_center": null, "period_from": "2026-04", "period_to": "2026-06"},
  "queries": [
    {"level": "B", "entity": "Elecar Northern Europe", "region": "NORTHERN-EUROPE",
     "region_name": "Northern Europe",
     "rows": [ ...the "results" array from the current-months query, untouched... ]},
    {"level": "B", "entity": "Elecar Northern Europe", "region": "NORTHERN-EUROPE",
     "region_name": "Northern Europe",
     "rows": [ ...the "results" array from the baseline-months query, untouched... ]}
  ]
}
```

`periods` (the current months) is required. One entry in `queries` per `sql_query` call:

| Field | Meaning |
|---|---|
| `level` | `A` regions (rows have `e3 entity`), `B` departments (rows have `d3 department`), `C` accounts (rows have `account is hierarchy`) |
| `entity` | The `e3 entity` value you sliced to (levels B and C) |
| `region`, `region_name` | Label and display name for that entity (levels B and C; level A derives them) |
| `department` | The `d3 department` value you sliced to (level C only) |
| `rows` | The `results` array exactly as returned. Do not edit, round, filter or de-duplicate it. |

Each row's `time` decides its side: a month in `periods` is actual, the same month
a year earlier is the baseline (`plan`). So a level-A query holding both years is
one entry. Only for a baseline from another version (same months) add
`"role": "plan"` to that entry, and `"role": "actual"` to the other.

The script removes Chimera's duplicate rows, keeps only operating-expense
accounts at level C, and pairs actual and baseline rows.

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
