#!/usr/bin/env python3
"""CI benchmark: compare all insert methods, output JSON + HTML report.

Runs after unit tests and integration tests pass.
Up to 10 methods when pyodbc + ODBC driver are available.

mssql-python (SA layer):
  1. insertmanyvalues           — SA 2.x default
  2. insertmanyvalues + sis     — SA default + use_setinputsizes=True
  3. executemany                 — SA with use_insertmanyvalues=False

sqlalchemy-mssql-bulkcopy:
  4. bulkcopy (direct)           — bulkcopy() function
  5. bulkcopy (hook)             — register_bulkcopy() event hook
  6. bulkcopy (pandas)           — df.to_sql(method=bulkcopy_insert_method)

mssql-python (raw driver):
  7. cursor.executemany (raw)    — raw driver, no SA overhead
  8. cursor.bulkcopy (raw)       — raw driver bulkcopy, theoretical ceiling

pyodbc (when available):
  9. pyodbc insertmanyvalues     — SA default on pyodbc
 10. pyodbc fast_executemany     — pyodbc's bulk parameter array mode

Outputs:
  benchmark_results.json    — raw + summary data
  benchmark_report.html     — dashboard with charts
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

URL = (
    "mssql+mssqlpython://sa:BenchMark!Pass123@localhost/master"
    "?Encrypt=yes&TrustServerCertificate=yes"
)
RAW_CONNSTR = (
    "SERVER=localhost,1433;DATABASE=master;UID=sa;PWD=BenchMark!Pass123;"
    "Encrypt=yes;TrustServerCertificate=yes;"
)
PYODBC_URL = (
    "mssql+pyodbc://sa:BenchMark!Pass123@localhost/master"
    "?driver=ODBC+Driver+18+for+SQL+Server"
    "&Encrypt=yes&TrustServerCertificate=yes"
)
TABLE = "__bcp_benchmark"
ROW_COUNTS = [1_000, 10_000, 100_000, 500_000]
ITERATIONS = 2

# Method display order — controls chart legend and grouping
METHOD_ORDER = [
    "insertmanyvalues",
    "executemany",
    "bulkcopy (direct)",
    "bulkcopy (hook)",
    "bulkcopy (pandas)",
    "cursor.executemany (raw)",
    "cursor.bulkcopy (raw)",
    "pyodbc insertmanyvalues",
    "pyodbc fast_executemany",
]


# ---------------------------------------------------------------------------
# Data generators — deterministic, same output every run
# ---------------------------------------------------------------------------
def generate_tuples(n: int) -> list[tuple[int, str]]:
    return [(i, f"user_{i:06d}") for i in range(n)]


def generate_rows(n: int) -> list[dict[str, object]]:
    return [{"id": i, "name": f"user_{i:06d}"} for i in range(n)]


def generate_df(n: int) -> pd.DataFrame:
    return pd.DataFrame({"id": range(n), "name": [f"user_{i:06d}" for i in range(n)]})


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def truncate(engine):
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE TABLE {TABLE}"))
        conn.commit()


def count_rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()


# ---------------------------------------------------------------------------
# Insert methods
# ---------------------------------------------------------------------------
def _exec_insert(engine, table, rows):
    with engine.begin() as conn:
        conn.execute(table.insert(), rows)


def _exec_bulkcopy_direct(engine, tuples, bulkcopy_fn):
    with engine.begin() as conn:
        bulkcopy_fn(
            conn,
            tuples,
            f"dbo.{TABLE}",
            batch_size=5_000,
            table_lock=True,
        )


def _exec_bulkcopy_hook(engine, table, rows):
    with engine.begin() as conn:
        conn.execute(
            table.insert().execution_options(use_bulkcopy=True),
            rows,
        )


def _exec_bulkcopy_pandas(engine, df, method_fn):
    df.to_sql(
        TABLE,
        engine,
        if_exists="append",
        index=False,
        method=method_fn,
    )


def _exec_raw_executemany(tuples):
    import mssql_python

    conn = mssql_python.connect(RAW_CONNSTR)
    cursor = conn.cursor()
    try:
        cursor.executemany(
            f"INSERT INTO {TABLE} (id, name) VALUES (?, ?)",
            tuples,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _exec_raw_bulkcopy(tuples):
    import mssql_python

    conn = mssql_python.connect(RAW_CONNSTR)
    cursor = conn.cursor()
    try:
        cursor.bulkcopy(
            f"dbo.{TABLE}",
            tuples,
            batch_size=5_000,
            table_lock=True,
        )
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    from sqlalchemy_mssql_bulkcopy import (
        bulkcopy,
        bulkcopy_insert_method,
        register_bulkcopy,
    )

    metadata = MetaData()
    t = Table(
        TABLE,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("name", String(50)),
    )

    # --- mssql-python engines ---
    engine = create_engine(URL)
    engine_em = create_engine(URL, use_insertmanyvalues=False)

    engine_hook = create_engine(URL, use_insertmanyvalues=False)
    register_bulkcopy(engine_hook, batch_size=5_000, table_lock=True)

    engine_pd = create_engine(URL, use_insertmanyvalues=False)
    register_bulkcopy(engine_pd, batch_size=5_000, table_lock=True)

    # --- pyodbc engines (optional) ---
    has_pyodbc = False
    engine_pyodbc = None
    engine_pyodbc_fast = None
    try:
        engine_pyodbc = create_engine(PYODBC_URL)
        engine_pyodbc_fast = create_engine(
            PYODBC_URL,
            use_insertmanyvalues=False,
            fast_executemany=True,
        )
        with engine_pyodbc.connect() as conn:
            conn.execute(text("SELECT 1"))
        has_pyodbc = True
        print("pyodbc + ODBC driver detected")
    except Exception as e:
        print(f"pyodbc not available, skipping: {e}")

    metadata.create_all(engine)

    all_results: list[dict] = []
    method_count = 9 if has_pyodbc else 7

    for n in ROW_COUNTS:
        print(f"\n--- {n:,d} rows ---")
        tuples = generate_tuples(n)
        rows = generate_rows(n)
        df = generate_df(n)

        for i in range(ITERATIONS):
            methods = [
                ("insertmanyvalues", lambda r=rows: _exec_insert(engine, t, r)),
                ("executemany", lambda r=rows: _exec_insert(engine_em, t, r)),
                (
                    "bulkcopy (direct)",
                    lambda tp=tuples: _exec_bulkcopy_direct(engine, tp, bulkcopy),
                ),
                (
                    "bulkcopy (hook)",
                    lambda r=rows: _exec_bulkcopy_hook(engine_hook, t, r),
                ),
                (
                    "bulkcopy (pandas)",
                    lambda d=df: _exec_bulkcopy_pandas(engine_pd, d, bulkcopy_insert_method),
                ),
                ("cursor.executemany (raw)", lambda tp=tuples: _exec_raw_executemany(tp)),
                ("cursor.bulkcopy (raw)", lambda tp=tuples: _exec_raw_bulkcopy(tp)),
            ]

            if has_pyodbc:
                methods.extend(
                    [
                        (
                            "pyodbc insertmanyvalues",
                            lambda r=rows: _exec_insert(engine_pyodbc, t, r),
                        ),
                        (
                            "pyodbc fast_executemany",
                            lambda r=rows: _exec_insert(engine_pyodbc_fast, t, r),
                        ),
                    ]
                )

            for method, run_fn in methods:
                truncate(engine)
                t0 = time.perf_counter()
                try:
                    run_fn()
                except Exception as e:
                    print(f"  ERR  {method:30s}  {e}")
                    all_results.append(
                        {
                            "method": method,
                            "rows": n,
                            "iteration": i,
                            "elapsed": 0,
                            "rows_per_sec": 0,
                            "correct": False,
                            "error": str(e),
                        }
                    )
                    continue
                elapsed = time.perf_counter() - t0
                got = count_rows(engine)
                rps = round(n / elapsed) if elapsed > 0 else 0
                ok = got == n
                print(
                    f"  {'OK' if ok else 'FAIL':4s}  {method:30s}  "
                    f"{elapsed:>7.3f}s  {rps:>10,d} rows/s"
                )
                all_results.append(
                    {
                        "method": method,
                        "rows": n,
                        "iteration": i,
                        "elapsed": round(elapsed, 4),
                        "rows_per_sec": rps,
                        "correct": ok,
                    }
                )

    # Cleanup
    t.drop(engine)
    engines = [engine, engine_em, engine_hook, engine_pd]
    if has_pyodbc:
        engines.extend([engine_pyodbc, engine_pyodbc_fast])
    for e in engines:
        e.dispose()

    # Summarize
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in all_results:
        if r.get("error"):
            continue
        grouped[(r["method"], r["rows"])].append(r)

    # Sort by method order, then row count
    def sort_key(item):
        (method, rows), _ = item
        try:
            idx = METHOD_ORDER.index(method)
        except ValueError:
            idx = 999
        return (rows, idx)

    print("\n" + "=" * 75)
    print(f"{'Method':<30s} {'Rows':>8s} {'Time':>8s} {'Rows/s':>10s} {'vs base':>8s}")
    print("=" * 75)

    summary = []
    # Collect baseline (insertmanyvalues) per row count
    baselines: dict[int, float] = {}
    for (method, rows), runs in grouped.items():
        if method == "insertmanyvalues":
            baselines[rows] = sum(r["rows_per_sec"] for r in runs) / len(runs)

    current_rows = 0
    for (method, rows), runs in sorted(grouped.items(), key=sort_key):
        if rows != current_rows:
            if current_rows != 0:
                print("-" * 75)
            current_rows = rows
        avg_elapsed = sum(r["elapsed"] for r in runs) / len(runs)
        avg_rps = round(sum(r["rows_per_sec"] for r in runs) / len(runs))
        correct = all(r["correct"] for r in runs)
        base = baselines.get(rows, 1)
        speedup = avg_rps / base if base > 0 else 0
        print(f"{method:<30s} {rows:>8,d} {avg_elapsed:>7.3f}s {avg_rps:>10,d} {speedup:>7.1f}x")
        summary.append(
            {
                "method": method,
                "rows": rows,
                "avg_elapsed": round(avg_elapsed, 4),
                "avg_rows_per_sec": avg_rps,
                "correct": correct,
                "order": METHOD_ORDER.index(method) if method in METHOD_ORDER else 999,
            }
        )

    # Save JSON
    output = {"summary": summary, "raw": all_results}
    Path("benchmark_results.json").write_text(json.dumps(output, indent=2))

    # Save HTML
    Path("benchmark_report.html").write_text(build_html(json.dumps(summary)))

    print("\nSaved: benchmark_results.json, benchmark_report.html")

    errors = [r for r in all_results if not r.get("correct", True)]
    if errors:
        print(f"\nFAILED: {len(errors)} incorrect results")
        for r in errors:
            print(f"  - {r['method']} @ {r['rows']} rows: {r.get('error', 'wrong count')}")
        sys.exit(1)

    print(f"\nPASS: all {method_count} methods correct across {len(ROW_COUNTS)} row counts")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
def build_html(summary_json: str) -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sqlalchemy-mssql-bulkcopy CI Benchmark</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600;800&display=swap');
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Inter',system-ui,sans-serif;background:#0a0a0f;color:#e0e0e8;min-height:100vh;padding:2rem}
  h1{font-size:1.5rem;font-weight:800;letter-spacing:-.02em;background:linear-gradient(135deg,#a78bfa,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:.3rem}
  .sub{color:#9898a8;font-size:.85rem;margin-bottom:2rem}
  .card{background:#12121c;border-radius:14px;padding:1.5rem;margin-bottom:1.5rem;border:1px solid #1e1e32}
  .card h2{font-size:.95rem;font-weight:600;margin-bottom:1rem;color:#c8c8d8}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  canvas{width:100%!important;height:320px!important}
  table{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:.8rem}
  th{text-align:left;padding:.6rem .8rem;border-bottom:2px solid #2a2a40;color:#9898b0;font-weight:600;text-transform:uppercase;font-size:.65rem;letter-spacing:.08em}
  td{padding:.5rem .8rem;border-bottom:1px solid #1a1a2e}
  tr:hover td{background:#16162a}
  .fastest{color:#34d399;font-weight:700}
  .slowest{color:#f87171}
  .row-sep td{border-top:2px solid #2a2a40;padding-top:.8rem}
  .badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.7rem;font-weight:600;white-space:nowrap}
  .b-sa{background:#1c1917;color:#a8a29e}
  .b-bcp{background:#1e1b4b;color:#a78bfa}
  .b-raw{background:#3b0764;color:#d8b4fe}
  .b-pyodbc{background:#451a03;color:#fbbf24}
  .speedup{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:.8rem}
  .legend-group{margin-top:1rem;display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;font-size:.75rem;color:#9898b0}
  .legend-group span{display:inline-flex;align-items:center;gap:.3rem}
  .legend-dot{width:10px;height:10px;border-radius:2px;display:inline-block}
</style>
</head>
<body>
<h1>sqlalchemy-mssql-bulkcopy</h1>
<div class="sub">CI Benchmark &mdash; SQL Server 2022 &mdash; rows/sec (higher is better)</div>

<div class="card">
  <h2>Scaling comparison</h2>
  <div class="grid">
    <div><canvas id="bar"></canvas></div>
    <div><canvas id="line"></canvas></div>
  </div>
</div>

<div class="card">
  <h2>All results</h2>
  <table>
    <thead><tr>
      <th>Method</th><th>Rows</th><th>Time</th>
      <th>Rows/sec</th><th>vs default</th>
    </tr></thead>
    <tbody id="tbl"></tbody>
  </table>
</div>

<script>
const D=__SUMMARY_JSON__;

// Colors by category
const P={
  'insertmanyvalues':'#78716c',
  'insertmanyvalues + sis':'#a8a29e',
  'executemany':'#57534e',
  'bulkcopy (direct)':'#a78bfa',
  'bulkcopy (hook)':'#8b5cf6',
  'bulkcopy (pandas)':'#c084fc',
  'cursor.executemany (raw)':'#94a3b8',
  'cursor.bulkcopy (raw)':'#e2e8f0',
  'pyodbc insertmanyvalues':'#f59e0b',
  'pyodbc fast_executemany':'#f97316'
};

// Method ordering
const MO=[
  'insertmanyvalues','insertmanyvalues + sis','executemany',
  'bulkcopy (direct)','bulkcopy (hook)','bulkcopy (pandas)',
  'cursor.executemany (raw)','cursor.bulkcopy (raw)',
  'pyodbc insertmanyvalues','pyodbc fast_executemany'
];

function badge(m){
  if(m.includes('pyodbc'))return 'b-pyodbc';
  if(m.includes('raw'))return 'b-raw';
  if(m.includes('bulkcopy'))return 'b-bcp';
  return 'b-sa';
}

const rc=[...new Set(D.map(r=>r.rows))].sort((a,b)=>a-b);
const ms=MO.filter(m=>D.some(r=>r.method===m));
const mx=Math.max(...rc);

// Bar chart — largest row count, sorted by speed
const bd=ms.map(m=>{
  const r=D.find(x=>x.method===m&&x.rows===mx);
  return{l:m,v:r?r.avg_rows_per_sec:0,c:P[m]||'#888'};
}).sort((a,b)=>b.v-a.v);

new Chart(document.getElementById('bar'),{
  type:'bar',
  data:{
    labels:bd.map(d=>d.l),
    datasets:[{data:bd.map(d=>d.v),backgroundColor:bd.map(d=>d.c),borderRadius:6}]
  },
  options:{
    responsive:true,
    plugins:{
      legend:{display:false},
      title:{display:true,text:mx.toLocaleString()+' rows — throughput',color:'#9898b0',font:{size:12}}
    },
    scales:{
      x:{ticks:{color:'#8888a0',font:{size:7},maxRotation:45},grid:{display:false}},
      y:{ticks:{color:'#8888a0',callback:v=>v>=1000?(v/1000).toFixed(0)+'K':v},grid:{color:'#1e1e32'}}
    }
  }
});

// Line chart — scaling across row counts, ordered by METHOD_ORDER
const ds=ms.map(m=>({
  label:m,
  data:rc.map(n=>{
    const r=D.find(x=>x.method===m&&x.rows===n);
    return r?r.avg_rows_per_sec:null;
  }),
  borderColor:P[m]||'#888',
  backgroundColor:'transparent',
  tension:.3,
  pointRadius:3,
  borderWidth:2
}));

new Chart(document.getElementById('line'),{
  type:'line',
  data:{labels:rc.map(n=>n>=1000?(n/1000)+'K':n),datasets:ds},
  options:{
    responsive:true,
    plugins:{
      legend:{position:'bottom',labels:{color:'#c8c8d8',font:{size:8},boxWidth:12,padding:8}},
      title:{display:true,text:'Scaling by row count',color:'#9898b0',font:{size:12}}
    },
    scales:{
      x:{title:{display:true,text:'Rows',color:'#666'},ticks:{color:'#8888a0'},grid:{color:'#1e1e32'}},
      y:{title:{display:true,text:'Rows/sec',color:'#666'},ticks:{color:'#8888a0',callback:v=>v>=1000?(v/1000).toFixed(0)+'K':v},grid:{color:'#1e1e32'}}
    }
  }
});

// Table — grouped by row count, sorted by speed within group
const tb=document.getElementById('tbl');
let prevRows=0;
rc.forEach(n=>{
  // Get baseline (insertmanyvalues) for this row count
  const baseRow=D.find(r=>r.method==='insertmanyvalues'&&r.rows===n);
  const base=baseRow?baseRow.avg_rows_per_sec:1;

  const rr=D.filter(r=>r.rows===n).sort((a,b)=>b.avg_rows_per_sec-a.avg_rows_per_sec);
  const fastest=Math.max(...rr.map(r=>r.avg_rows_per_sec));
  const slowest=Math.min(...rr.map(r=>r.avg_rows_per_sec));

  rr.forEach((r,i)=>{
    const sp=(r.avg_rows_per_sec/base).toFixed(1);
    const cls=r.avg_rows_per_sec===fastest?'fastest':r.avg_rows_per_sec===slowest?'slowest':'';
    const sep=(i===0&&prevRows!==0)?'row-sep':'';
    const rpsStr=r.avg_rows_per_sec>=1000?
      (r.avg_rows_per_sec/1000).toFixed(1)+'K':
      r.avg_rows_per_sec.toLocaleString();
    const rowsStr=r.rows>=1000?(r.rows/1000)+'K':r.rows;
    tb.innerHTML+=`<tr class="${sep}">
      <td><span class="badge ${badge(r.method)}">${r.method}</span></td>
      <td>${rowsStr}</td>
      <td class="${cls}">${r.avg_elapsed}s</td>
      <td class="${cls}">${rpsStr}</td>
      <td><span class="speedup">${sp}x</span></td>
    </tr>`;
  });
  prevRows=n;
});
</script>
</body>
</html>""".replace("__SUMMARY_JSON__", summary_json)


if __name__ == "__main__":
    main()
