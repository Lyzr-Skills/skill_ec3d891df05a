#!/usr/bin/env python3
"""Deterministic opex variance analysis for the FP&A Variance Agent (contract v1.0).

Reads one JSON document (file path argument, or stdin) holding the rows fetched
from Anaplan, and prints {"data": <contract data object>, "facts": {...}}.
`data` drops straight into the response contract; `facts` are pre-computed,
pre-formatted figures the agent may quote in `answer` and `key_points`.

The LLM never does arithmetic: every number in `data` comes from here.
Standard library only, so it runs in any Python 3.9+ sandbox.

Input shape (all row arrays optional, but at least one must be non-empty):
{
  "fiscal_period": "FY2026 Q2",
  "currency": "USD",
  "periods": ["Apr 26", "May 26", "Jun 26"],          # or "2026-04" style
  "filters": {"region": null, "department": null, "account": null,
              "cost_center": null, "period_from": null, "period_to": null},
  "region_rows":     [ROW, ...],   # level A: region x month, all departments
  "department_rows": [ROW, ...],   # level B: region x department x month
  "account_rows":    [ROW, ...]    # level C: region x department x account x month
}
ROW = {"period": "Apr 26", "entity": "Elecar EURO", "region": "EURO",
       "department": "Field Marketing", "department_group": "Sales & Marketing",
       "account": "6001 Advertising", "plan": 123.4, "actual": 150.0}
"plan" is the baseline amount (prior-year actuals, or a plan version);
"actual" is the current amount. Variance = actual - plan (positive = overspend).
"""
import json
import re
import sys
from collections import OrderedDict

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# Histogram bins from the contract example, lowest to highest.
BINS = [
    ("< -20%", None, -20.0),
    ("-20% to -10%", -20.0, -10.0),
    ("-10% to -5%", -10.0, -5.0),
    ("-5% to 0%", -5.0, 0.0),
    ("0% to 5%", 0.0, 5.0),
    ("5% to 10%", 5.0, 10.0),
    ("10% to 20%", 10.0, 20.0),
    ("20% to 50%", 20.0, 50.0),
    (">= 50%", 50.0, None),
]
OUTLIER_PCT = 15.0      # |variance %| that makes a monthly row an outlier
ONE_OFF_SHARE = 0.80    # one month carries >= 80% of a line's variance
RUN_RATE_PCT = 8.0      # every month off plan, same direction, by >= 8%
ALL_OPEX = "All operating expenses"


def to_period(label):
    """'Apr 26' -> '2026-04'; '2026-04' passes through."""
    label = str(label).strip()
    if re.fullmatch(r"\d{4}-\d{2}", label):
        return label
    m = re.fullmatch(r"([A-Za-z]{3})\s+(\d{2})", label)
    if not m or m.group(1).title() not in MONTHS:
        raise ValueError(f"unrecognised period label: {label!r}")
    return f"20{m.group(2)}-{MONTHS[m.group(1).title()]:02d}"


def code(text):
    text = re.sub(r"^Elecar\s+", "", str(text))
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")


def pct(variance, plan):
    if not plan:
        return None
    return round(variance / abs(plan) * 100.0, 1)


def amounts(plan, actual):
    plan, actual = round(plan), round(actual)
    var = actual - plan
    return {"plan": plan, "actual": actual, "variance": var,
            "variance_pct": pct(var, plan)}


def fmt_amount(value, currency, signed=True):
    sign = ("+" if value > 0 else "-" if value < 0 else "") if signed else ("-" if value < 0 else "")
    v = abs(value)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            num = f"{v / div:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{num}{suffix} {currency}"
    return f"{sign}{v:.0f} {currency}"


def clean(rows, level):
    """Drop Chimera's duplicate rows, then sum entities into region labels."""
    seen, out = set(), OrderedDict()
    for r in rows or []:
        period = to_period(r["period"])
        raw_key = (period, r.get("entity") or r.get("region"),
                   r.get("department"), r.get("account"),
                   round(float(r.get("plan") or 0), 2),
                   round(float(r.get("actual") or 0), 2))
        if raw_key in seen:          # exact duplicate from the same query
            continue
        seen.add(raw_key)
        region = r.get("region") or code(r.get("entity", "ALL"))
        dept = r.get("department") if level != "A" else None
        account = r.get("account") if level == "C" else None
        key = (period, region, dept, account)
        row = out.setdefault(key, {
            "period": period, "region": region, "department": dept,
            "region_name": r.get("region_name") or region,
            "department_group": r.get("department_group"),
            "account": account or ALL_OPEX, "plan": 0.0, "actual": 0.0})
        row["plan"] += float(r.get("plan") or 0)
        row["actual"] += float(r.get("actual") or 0)
    for row in out.values():
        row["variance"] = row["actual"] - row["plan"]
        row["variance_pct"] = pct(row["variance"], row["plan"])
        if row["department"]:
            row["cost_center"] = f"{row['region']}-{code(row['department'])}"
            row["cost_center_name"] = f"{row['region_name']} {row['department']}"
        else:
            row["cost_center"] = row["region"]
            row["cost_center_name"] = f"{row['region_name']} (all departments)"
    return list(out.values())


def pattern(monthly, n_periods):
    if n_periods <= 1:
        return "single_period"
    abs_total = sum(abs(m["variance"]) for m in monthly)
    if abs_total and max(abs(m["variance"]) for m in monthly) >= ONE_OFF_SHARE * abs_total:
        return "one_off"
    pcts = [m["variance_pct"] for m in monthly]
    if len(monthly) == n_periods and all(p is not None for p in pcts):
        if all(p >= RUN_RATE_PCT for p in pcts) or all(p <= -RUN_RATE_PCT for p in pcts):
            return "run_rate"
    return "mixed"


def drivers(rows, n_periods, total_variance):
    lines = OrderedDict()
    for r in rows:
        lines.setdefault((r["cost_center"], r["account"]), []).append(r)
    direction = 1 if total_variance >= 0 else -1
    ranked = []
    for (cc, account), monthly in lines.items():
        plan = sum(m["plan"] for m in monthly)
        actual = sum(m["actual"] for m in monthly)
        peak = max(monthly, key=lambda m: abs(m["variance"]))
        first = monthly[0]
        item = {"cost_center": cc, "cost_center_name": first["cost_center_name"],
                "region": first["region"],
                "department": first["department_group"] or first["department"] or ALL_OPEX,
                "account": account}
        item.update(amounts(plan, actual))
        item["pattern"] = pattern(monthly, n_periods)
        item["peak_period"] = peak["period"]
        ranked.append(item)
    ranked.sort(key=lambda d: d["variance"] * direction, reverse=True)
    return [d for d in ranked if d["variance"] * direction > 0][:3]


def histogram(rows):
    counts = [0] * len(BINS)
    for r in rows:
        p = r["variance_pct"]
        if p is None:
            continue
        for i, (_, lo, hi) in enumerate(BINS):
            if (lo is None or p >= lo) and (hi is None or p < hi):
                counts[i] += 1
                break
    return {"metric": "variance_pct", "unit": "percent of plan",
            "bins": [{"label": lbl, "lower": lo, "upper": hi, "count": c}
                     for (lbl, lo, hi), c in zip(BINS, counts)]}


def outliers(rows):
    picked = [r for r in rows
              if (r["variance_pct"] is None and r["actual"])
              or (r["variance_pct"] is not None and abs(r["variance_pct"]) >= OUTLIER_PCT)]
    picked.sort(key=lambda r: abs(r["variance"]), reverse=True)
    return [{"period": r["period"], "cost_center": r["cost_center"],
             "cost_center_name": r["cost_center_name"], "account": r["account"],
             "variance": round(r["variance"]), "variance_pct": r["variance_pct"]}
            for r in picked[:5]]


DEPARTMENT_GROUPS = {
    "Direct Sales": "Sales & Marketing", "Inside Sales": "Sales & Marketing",
    "Field Marketing": "Sales & Marketing", "Professional Services": "Sales & Marketing",
    "Manufacturing": "Operations", "Warehouse / DC": "Operations",
    "Customer Care": "Operations", "Quality Assurance": "Operations",
    "Product Engineering": "R&D", "Product Management": "R&D",
    "Marketing": "Corporate", "Finance": "Corporate", "Information Technology": "Corporate",
    "Legal": "Corporate", "Administration": "Corporate", "Human Resources": "Corporate",
    "Executive": "Corporate",
}
LEVEL_KEYS = {"A": "region_rows", "B": "department_rows", "C": "account_rows"}


# Leaf accounts under "Operating Expenses" in the IFP model's Account IS Hierarchy.
OPEX_ACCOUNTS = {
    # Employee Costs
    "5000 Salaries & Wages", "5010 Sales Commissions", "5100 Productivity Bonus",
    "5101 Quarterly MBO", "5102 Year End Bonus", "5200 Health Insurance",
    "5201 Retirement Plan", "5202 Payroll Tax", "5203 Unemployment Insurance/Tax",
    "5300 Stock-Based Compensation", "5301 Life Insurance", "5302 Long Term Disability",
    "5307 Severance", "5400 Workers Compensation",
    # General Operating Expenses
    "6001 Advertising", "6002 Consulting Fees", "6003 Promotions", "6004 Legal Fees",
    "6005 Office Equipment", "6006 Computer Supplies", "6101 Software",
    "6102 Licenses/Permits", "6103 Staff Functions/Relations", "6104 Meals/Entertainment",
    "6105 Recruitment", "6200 Miscellaneous Expense", "6301 Travel Expense", "6302 Training",
    "6401 Telecom Subscriptions", "6501 Dues and Subscriptions",
    "6502 Charitable Contributions", "6503 Office Supplies", "6601 Rent", "6602 Utilities",
    "6700 Repairs & Maintenance", "6750 Operating Leases",
    # Depreciation and Amortization
    "7101 Amortization Expense", "7102 Depreciation Expense",
    "7103 Amortization charge of consolidated goodwill",
    "7104 Write-Down/ Write back negative goodwill",
    # Allocated Expenses
    "7000 Allocated Sales", "7001 Marketing Allocation", "7002 IT Allocation",
    "7003 Finance Allocation", "7004 HR Allocation",
}


def is_opex_account(label):
    return label in OPEX_ACCOUNTS


def rows_from_queries(queries):
    """Turn raw sql_query results into paired plan/actual ROWs, grouped by level.

    Each query = {"level": "A"|"B"|"C", "role": "actual"|"plan",
                  "period": <ACTUAL month this pairs with, e.g. "Apr 26">,
                  "entity": "Elecar EURO" (B/C), "region": "EURO" (optional),
                  "department": "Field Marketing" (C only),
                  "rows": <the "results" array returned by sql_query>}
    """
    paired = {lvl: OrderedDict() for lvl in LEVEL_KEYS}
    for q in queries:
        lvl, role = q["level"], q["role"]
        seen = set()
        dim = {"A": "e3 entity", "B": "d3 department", "C": "account is hierarchy"}[lvl]
        for raw in q.get("rows") or []:
            # Chimera can return a row twice, and the copies may differ in the last
            # float digit, so de-duplicate on the dimension member, never the value.
            if raw[dim] in seen:
                continue
            seen.add(raw[dim])
            amount = float(raw.get("reporting amounts") or 0)
            if lvl == "A":
                entity = raw["e3 entity"]
                region, dept, account = q.get("region_map", {}).get(entity) or code(entity), None, None
            elif lvl == "B":
                entity, dept, account = q["entity"], raw["d3 department"], None
                region = q.get("region") or code(entity)
            else:
                entity, dept, account = q["entity"], q["department"], raw["account is hierarchy"]
                region = q.get("region") or code(entity)
                if not is_opex_account(account):
                    continue
            key = (q["period"], entity, dept, account)
            row = paired[lvl].setdefault(key, {
                "period": q["period"], "entity": entity, "region": region,
                "region_name": q.get("region_name") or (re.sub(r"^Elecar\s+", "", entity) if lvl == "A" else region),
                "department": dept, "department_group": DEPARTMENT_GROUPS.get(dept, dept),
                "account": account, "plan": 0.0, "actual": 0.0})
            row[role] += amount
    return {LEVEL_KEYS[lvl]: list(rows.values()) for lvl, rows in paired.items()}


def analyse(doc):
    if doc.get("queries"):
        doc = dict(doc, **rows_from_queries(doc["queries"]))
    levels = {"A": clean(doc.get("region_rows"), "A"),
              "B": clean(doc.get("department_rows"), "B"),
              "C": clean(doc.get("account_rows"), "C")}
    if not any(levels.values()):
        raise ValueError("no rows supplied")
    periods = [to_period(p) for p in doc.get("periods") or []] or \
        sorted({r["period"] for rows in levels.values() for r in rows})
    n = len(periods)
    total_rows = levels["A"] or levels["B"] or levels["C"]
    hist_rows = levels["B"] or levels["A"] or levels["C"]
    detail_rows = levels["C"] or levels["B"] or levels["A"]

    totals = amounts(sum(r["plan"] for r in total_rows), sum(r["actual"] for r in total_rows))
    top = drivers(detail_rows, n, totals["variance"])
    filters = {k: None for k in ("region", "department", "account", "cost_center",
                                 "period_from", "period_to")}
    filters.update({k: v for k, v in (doc.get("filters") or {}).items() if k in filters})
    currency = doc.get("currency", "USD")
    data = {
        "scope": {"fiscal_period": doc.get("fiscal_period"), "currency": currency,
                  "periods": periods, "filters": filters},
        "totals": totals,
        "top_drivers": top,
        "histogram": histogram(hist_rows),
        "outliers": outliers(detail_rows),
    }

    by_region = OrderedDict()
    for r in total_rows:
        agg = by_region.setdefault(r["region"], [0.0, 0.0])
        agg[0] += r["plan"]
        agg[1] += r["actual"]
    top_share = None
    if totals["variance"]:
        top_share = round(sum(d["variance"] for d in top) / totals["variance"] * 100, 1)
    facts = {
        "direction": "over" if totals["variance"] > 0 else "under" if totals["variance"] < 0 else "on",
        "total_variance_fmt": fmt_amount(totals["variance"], currency),
        "plan_fmt": fmt_amount(totals["plan"], currency, signed=False),
        "actual_fmt": fmt_amount(totals["actual"], currency, signed=False),
        "variance_pct": totals["variance_pct"],
        "top_drivers_share_of_variance_pct": top_share,
        "top_drivers_fmt": [f"{d['cost_center_name']} / {d['account']}: "
                            f"{fmt_amount(d['variance'], currency)} ({d['variance_pct']}%)"
                            for d in top],
        "by_region": [{"region": k, **amounts(*v),
                       "variance_fmt": fmt_amount(round(v[1]) - round(v[0]), currency)}
                      for k, v in by_region.items()],
        "rows_analysed": {k: len(v) for k, v in levels.items()},
    }
    return {"data": data, "facts": facts}


def main():
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(analyse(json.loads(raw)), indent=2))


if __name__ == "__main__":
    main()
