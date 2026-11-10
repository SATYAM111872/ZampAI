"""Batch runner: one pipeline run per CSV row.

    python batch.py prospects.csv [--out results.csv] [--start N]

CSV with a header row (case-insensitive): company (required), name, role,
website, notes. Each row needs the name, the role, or both; Stage 1 looks up
the missing one by search. Every row is validated before anything runs, so a
bad row stops the batch at the start, not halfway through.

Rows run one after another (parallel searches get rate-limited). Each run is
stored like a UI run, so drafts are reviewed in the Dashboard. A summary CSV
is written after every row; if the batch stops, rerun with --start N.
"""

import argparse
import csv
import sys
import time
from pathlib import Path

import agent
import store

FIELDS = ("name", "company", "role", "website", "notes")
OUT_COLS = ["row", "run_id", "company", "name", "role", "looked_up", "status", "rounds", "subject", "error", "seconds"]


def read_rows(path):
    """Returns [(row_number, prospect_dict)], row numbers counting data rows from 1."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = {h.strip().lower(): h for h in (reader.fieldnames or []) if h}
            if "company" not in header:
                sys.exit(f"{path}: needs a header row with a 'company' column "
                         f"(found: {', '.join(reader.fieldnames or []) or 'nothing'})")
            rows = []
            for i, r in enumerate(reader, 1):
                p = {k: (r.get(header[k]) or "").strip() if k in header else "" for k in FIELDS}
                if any(p.values()):  # skip blank lines
                    rows.append((i, p))
    except UnicodeDecodeError:
        sys.exit(f"{path}: not UTF-8. In Excel, save as 'CSV UTF-8 (Comma delimited)'.")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--out", help="summary CSV (default: <input>_results.csv)")
    ap.add_argument("--start", type=int, default=1, help="first row to run (to resume a stopped batch)")
    a = ap.parse_args()

    rows = read_rows(a.csv)
    if not rows:
        sys.exit(f"{a.csv}: no data rows")
    bad = [(i, p, agent.validate_input(p)) for i, p in rows]
    bad = [b for b in bad if b[2]]
    if bad:
        for i, p, missing in bad:
            print(f"row {i} ({p['company'] or 'no company'}): missing {', '.join(missing)}", file=sys.stderr)
        sys.exit(f"{len(bad)} invalid row(s); nothing was run. Each row needs company plus name or role.")

    todo = [(i, p) for i, p in rows if i >= a.start]
    out = Path(a.out) if a.out else Path(a.csv).with_name(Path(a.csv).stem + "_results.csv")
    resume = a.start > 1 and out.exists()
    if resume:
        with open(out, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        if header != OUT_COLS:
            sys.exit(f"{out} has different columns (written by an older version). Resume into a new file "
                     f"with --out, e.g. --out {out.with_name(out.stem + '_2.csv')}")
    print(f"{len(todo)} prospects to run -> {out}" + (" (appending)" if resume else ""))

    counts = {}
    with open(out, "a" if resume else "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, OUT_COLS)
        if not resume:
            w.writeheader()
        for i, p in todo:
            t = time.time()
            try:
                rid = agent.run_pipeline(p)
            except KeyboardInterrupt:
                sys.exit(f"\nStopped at row {i} (that run is marked failed). "
                         f"Resume with: python batch.py {a.csv} --start {i}")
            run = store.get_run(rid)
            d = store.current_draft(rid)
            res = run.get("resolved") or {}
            looked_up = f"{res['field']}: {'found' if res.get('value') else 'not found'}" if res else ""
            w.writerow({"row": i, "run_id": rid, "company": run["company"], "name": run["name"],
                        "role": run["role"], "looked_up": looked_up, "status": run["status"],
                        "rounds": run.get("rounds") or "",
                        "subject": d["subject"] if d else "", "error": run["error"] or "",
                        "seconds": round(time.time() - t, 1)})
            f.flush()
            counts[run["status"]] = counts.get(run["status"], 0) + 1
            print(f"[row {i}, {sum(counts.values())}/{len(todo)}] {run['company']}: "
                  f"{run['name'] or '?'} ({run['role'] or '?'}) -> {run['status']}"
                  + (f" ({looked_up})" if looked_up else "") + (f" - {run['error']}" if run["error"] else ""),
                  flush=True)

    print("\nDone: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
          + ". Review drafts in the Dashboard (streamlit run main.py).")


if __name__ == "__main__":
    main()
