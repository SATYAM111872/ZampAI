"""Batch runner: one pipeline run per CSV row.

    python batch.py prospects.csv [--out results.csv] [--start N]

CSV with a header row (case-insensitive): company (required), name, role,
website, notes. Each row needs the name, the role, or both; Stage 1 looks up
the missing one by search. Every row is validated before anything runs, so a
bad row stops the batch at the start, not halfway through.

Rows run one after another (parallel searches get rate-limited). Each run is
stored like a UI run, so drafts are reviewed in the Dashboard. A summary CSV
is written after every row; if the batch stops, rerun with --start N.

The Streamlit "Batch" page uses the same parsing and result rows, and runs
the rows in a background thread (BatchJob) so page reruns do not stop it.
"""

import argparse
import csv
import io
import sys
import threading
import time
from pathlib import Path

import agent
import store

FIELDS = ("name", "company", "role", "website", "notes")
OUT_COLS = ["row", "run_id", "company", "name", "role", "looked_up", "status", "rounds", "subject", "error", "seconds"]


def parse_rows(lines):
    """lines: an open text file or any iterable of CSV lines. Returns
    [(row_number, prospect_dict)], row numbers counting data rows from 1.
    Raises ValueError if there is no 'company' column."""
    reader = csv.DictReader(lines)
    header = {h.strip().lower(): h for h in (reader.fieldnames or []) if h}
    if "company" not in header:
        raise ValueError("needs a header row with a 'company' column "
                         f"(found: {', '.join(reader.fieldnames or []) or 'nothing'})")
    rows = []
    for i, r in enumerate(reader, 1):
        p = {k: (r.get(header[k]) or "").strip() if k in header else "" for k in FIELDS}
        if any(p.values()):  # skip blank lines
            rows.append((i, p))
    return rows


def read_rows(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return parse_rows(f)
    except UnicodeDecodeError:
        sys.exit(f"{path}: not UTF-8. In Excel, save as 'CSV UTF-8 (Comma delimited)'.")
    except ValueError as e:
        sys.exit(f"{path}: {e}")


def invalid_rows(rows):
    """[(row_number, prospect, missing_fields)] for rows that would not run."""
    return [(i, p, m) for i, p in rows if (m := agent.validate_input(p))]


def result_row(i, rid, seconds):
    """One summary row (OUT_COLS) for a finished run."""
    run = store.get_run(rid)
    d = store.current_draft(rid)
    res = run.get("resolved") or {}
    looked_up = f"{res['field']}: {'found' if res.get('value') else 'not found'}" if res else ""
    return {"row": i, "run_id": rid, "company": run["company"], "name": run["name"],
            "role": run["role"], "looked_up": looked_up, "status": run["status"],
            "rounds": run.get("rounds") or "",
            "subject": d["subject"] if d else "", "error": run["error"] or "",
            "seconds": seconds}


def results_csv(results):
    buf = io.StringIO()
    w = csv.DictWriter(buf, OUT_COLS)
    w.writeheader()
    w.writerows(results)
    return buf.getvalue()


class BatchJob:
    """Runs rows one after another in a background thread, so it keeps going
    when the Streamlit page reruns. stop() takes effect after the current row
    (a run in progress is not cut off). The thread is a daemon: stopping the
    Streamlit server ends it, and the run in progress is left as 'running'."""

    def __init__(self, rows, label=""):
        self.rows, self.label = rows, label
        self.results, self.current = [], None   # current = (row_number, prospect, start time)
        self.started, self.finished = time.time(), None
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="batch", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.stop_requested = True

    @property
    def running(self):
        return self._thread.is_alive()

    def snapshot(self):
        with self._lock:
            return list(self.results)

    def _run(self):
        try:
            for i, p in self.rows:
                if self.stop_requested:
                    break
                t = time.time()
                self.current = (i, p, t)
                try:
                    rid = agent.run_pipeline(p)
                    row = result_row(i, rid, round(time.time() - t, 1))
                except Exception as e:  # run_pipeline closes its own failures; this is a bug or DB error
                    row = {"row": i, "company": p["company"], "name": p["name"], "role": p["role"],
                           "status": "failed", "error": f"{type(e).__name__}: {e}",
                           "seconds": round(time.time() - t, 1)}
                with self._lock:
                    self.results.append(row)
        finally:
            self.current = None
            self.finished = time.time()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--out", help="summary CSV (default: <input>_results.csv)")
    ap.add_argument("--start", type=int, default=1, help="first row to run (to resume a stopped batch)")
    a = ap.parse_args()

    rows = read_rows(a.csv)
    if not rows:
        sys.exit(f"{a.csv}: no data rows")
    bad = invalid_rows(rows)
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
    print("Generic fallback is " + ("ON: prospects with no hook get a generic draft (status 'generic')."
          if agent.generic_fallback_on() else "OFF: prospects with no hook stop at needs_research.")
          + " Change it with the switch on the Dashboard.")

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
            row = result_row(i, rid, round(time.time() - t, 1))
            w.writerow(row)
            f.flush()
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            print(f"[row {i}, {sum(counts.values())}/{len(todo)}] {row['company']}: "
                  f"{row['name'] or '?'} ({row['role'] or '?'}) -> {row['status']}"
                  + (f" ({row['looked_up']})" if row["looked_up"] else "")
                  + (f" - {row['error']}" if row["error"] else ""),
                  flush=True)

    print("\nDone: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
          + ". Review drafts in the Dashboard (streamlit run main.py).")


if __name__ == "__main__":
    main()
