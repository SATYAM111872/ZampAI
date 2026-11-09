"""Outreach pipeline: Stages 1-7 of FLOWCHART.md, plus the Stage 8 review actions.

Each stage is one function that reads/writes the run state and emits a
StageEvent (started / done / failed / skipped). The UI renders the events live;
the dashboard replays the same stored events.

CLI:  python agent.py "Prospect Name" "Company" "Role" [--website URL] [--notes TEXT]
"""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import date, datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

import config as C
import prompts as PR
import store
from llm import llm
from search import search as search_tool

store.init_db()

STAGE_NAMES = {1: "Input", 2: "Plan queries", 3: "Gather sources", 4: "Extract signals",
               5: "Score & pick hook", 6: "Draft", 7: "Check draft", 8: "Review"}
TERMINAL = {"drafted", "flagged", "needs_research", "failed", "approved", "discarded"}
NOTES_URL = "rep-notes://pasted"


class StageError(Exception):
    """A stage failed (error or timeout). The run goes to status `failed`."""


# =============================================================================
# LLM schemas
# =============================================================================
class ExtractedSignal(BaseModel):
    type: Literal[tuple(C.SIGNAL_TYPES)]
    level: Literal["person", "company"]
    summary: str
    evidence: str
    source_url: str
    date: Optional[str] = Field(None, description="YYYY-MM-DD, or null if the source shows no date")


class Extraction(BaseModel):
    signals: list[ExtractedSignal]


class Rating(BaseModel):
    signal_id: int
    R: int = Field(description="relevance 0-3")
    reason: str


class Ratings(BaseModel):
    ratings: list[Rating]


class DraftParts(BaseModel):
    subject: str
    hook: str
    bridge: str
    value: str
    cta: str


class Claim(BaseModel):
    claim: str
    supported: bool
    support: str = Field(description="what in the signal/profile supports it, or why not")


class Grounding(BaseModel):
    claims: list[Claim]


_llm_pool = ThreadPoolExecutor(max_workers=4)


_TRANSIENT = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "high demand")


def call_llm(schema, system, user, timeout=C.LLM_TIMEOUT_S):
    """Structured LLM call with a hard overall timeout. Retries transient
    overload/rate-limit errors with backoff. Raises StageError otherwise."""
    chain = llm.with_structured_output(schema)
    deadline = time.time() + timeout
    last = None
    for attempt, backoff in enumerate(C.LLM_RETRY_BACKOFF_S + [None]):
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        fut = _llm_pool.submit(chain.invoke, [("system", system), ("human", user)])
        try:
            out = fut.result(timeout=remaining)
        except FutTimeout:
            raise StageError(f"LLM timed out after {timeout}s")
        except Exception as e:  # network, quota, parse errors
            last = f"{type(e).__name__}: {str(e)[:300]}"
            if backoff is not None and any(t in str(e) for t in _TRANSIENT) and time.time() + backoff < deadline:
                time.sleep(backoff)
                continue
            raise StageError(f"LLM error: {last}")
        if out is None:
            raise StageError("LLM returned no parseable structured output")
        return out
    raise StageError(f"LLM error after retries: {last}")


# =============================================================================
# Events
# =============================================================================
class Emitter:
    """Stores finished StageEvents and forwards every event to the UI callback."""

    def __init__(self, run_id, on_event=None):
        self.run_id = run_id
        self.on_event = on_event or (lambda ev: None)
        self._open = {}  # stage -> (t0, started_iso, label)

    def _notify(self, ev):
        # A rendering bug in the UI must not fail the pipeline. (Streamlit's rerun/stop
        # signals are BaseExceptions and still propagate; callers handle those.)
        try:
            self.on_event(ev)
        except Exception as e:
            print(f"[ui callback error ignored] {type(e).__name__}: {e}")

    def start(self, stage, label=None):
        label = label or STAGE_NAMES[stage]
        self._open[stage] = (time.time(), store.now_iso(), label)
        store.update_run(self.run_id, current_stage=stage)
        self._notify({"run_id": self.run_id, "stage": stage, "stage_name": label,
                      "status": "started", "summary": "running...", "payload": None})

    def finish(self, stage, status, summary, payload=None, label=None):
        t, started_iso, open_label = self._open.pop(stage, (time.time(), store.now_iso(), None))
        ev = {"run_id": self.run_id, "stage": stage, "stage_name": label or open_label or STAGE_NAMES[stage],
              "status": status, "started_at": started_iso, "ended_at": store.now_iso(),
              "elapsed": round(time.time() - t, 2), "summary": summary, "payload": payload}
        store.add_event(ev)
        self._notify(ev)
        return ev

    def fail_open(self, summary):
        """Close every stage still open (after an error/interrupt) as failed.
        Returns the stage numbers closed."""
        stages = list(self._open)
        for st in stages:
            try:
                self.finish(st, "failed", summary)
            except BaseException:
                pass
        return stages


# =============================================================================
# Helpers
# =============================================================================
def norm_url(u):
    u = (u or "").strip()
    if u == NOTES_URL:
        return u
    try:
        p = urlsplit(u)
    except ValueError:
        return u.lower()
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return urlunsplit((p.scheme.lower() or "https", host, path, p.query, ""))


def domain_of(website):
    w = (website or "").strip()
    if not w:
        return ""
    if "://" not in w:
        w = "https://" + w
    return urlsplit(w).netloc.lower().removeprefix("www.")


def parse_date(s):
    """Parse an ISO-ish date string to a date, or None."""
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:25]).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "to", "in", "and", "for", "on", "its", "is", "at", "by", "with",
         "as", "has", "from", "that", "it", "this", "was", "be", "new"}


def words(s):
    return [w for w in _WORD.findall((s or "").lower())]


def jaccard(a, b, exclude=frozenset()):
    sa = set(words(a)) - _STOP - exclude
    sb = set(words(b)) - _STOP - exclude
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def word_count(s):
    return len(re.findall(r"\b[\w'’-]+\b", s or ""))


def seller_text():
    s = C.SELLER_PROFILE
    lines = [f"Product: {s['product']} - {s['one_liner']}",
             f"Buyers: {', '.join(s['buyers'])}",
             "Problems solved:", *[f"- {p}" for p in s["problems_solved"]],
             "Proof points:", *([f"- {p}" for p in s["proof_points"]] or ["- (none; do not cite any)"])]
    return "\n".join(lines)


def assemble(prospect, parts):
    first = prospect["name"].split()[0]
    body = (f"Hi {first},\n\n{parts['hook'].strip()} {parts['bridge'].strip()}\n\n"
            f"{parts['value'].strip()}\n\n{parts['cta'].strip()}\n\n{C.SELLER_PROFILE['sign_off']}")
    return body


def core_text(parts):
    return " ".join(parts[k].strip() for k in ("hook", "bridge", "value", "cta"))


# =============================================================================
# Stage 1 - Input
# =============================================================================
REQUIRED = ("name", "company", "role")


def validate_input(p):
    """Returns a list of missing required fields (empty = valid)."""
    return [f for f in REQUIRED if not (p.get(f) or "").strip()]


# =============================================================================
# Stage 2 - Plan queries (code)
# =============================================================================
def plan_queries(p):
    fill = {"name": p["name"].strip(), "company": p["company"].strip(),
            "function": C.SELLER_PROFILE["function"]}
    qs = [{"bucket": "company", "query": t.format(**fill)} for t in C.COMPANY_QUERIES]
    qs += [{"bucket": "person", "query": t.format(**fill)} for t in C.PERSON_QUERIES]
    dom = domain_of(p.get("website"))
    if dom:
        qs.append({"bucket": "company", "query": C.WEBSITE_QUERY.format(domain=dom)})
    return qs


# =============================================================================
# Stage 3 - Gather sources (search tool)
# =============================================================================
def gather_sources(queries, notes):
    per_query, errors = {}, {}
    pool = ThreadPoolExecutor(max_workers=C.SEARCH_WORKERS)
    try:
        futs = {q["query"]: pool.submit(search_tool, q) for q in queries}
        # Each query may walk up to 4 backends; bound the whole stage.
        deadline = time.time() + C.SEARCH_TIMEOUT_S * 4
        for qtext, fut in futs.items():
            try:
                per_query[qtext] = fut.result(timeout=max(1, deadline - time.time()))
            except FutTimeout:
                errors[qtext] = "timeout"
                per_query[qtext] = []
            except Exception as e:
                errors[qtext] = f"{type(e).__name__}: {e}"[:600]
                per_query[qtext] = []
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # don't block on timed-out searches

    if len(errors) == len(queries) and not (notes or "").strip():
        raise StageError(f"all {len(queries)} searches failed: {list(errors.values())[0]}")

    # Round-robin across queries so every query contributes before the cap; dedupe URLs.
    seen, sources = set(), []
    lists = [list(v) for v in per_query.values()]
    while any(lists) and len(sources) < C.MAX_SOURCES:
        for lst in lists:
            if lst and len(sources) < C.MAX_SOURCES:
                s = lst.pop(0)
                key = norm_url(s["url"])
                if key not in seen:
                    seen.add(key)
                    sources.append(s)
    if (notes or "").strip():
        sources.append({"url": NOTES_URL, "title": "Notes pasted by rep", "snippet": notes.strip(),
                        "published_date": None, "query": None, "origin": "rep_provided"})
    counts = {q: len(v) for q, v in per_query.items()}
    backends = {}
    for s in sources:
        b = s.get("backend") or s["origin"]
        backends[b] = backends.get(b, 0) + 1
    return sources, {"per_query": counts, "backends": backends}, errors


# =============================================================================
# Stage 4 - Extract signals (LLM) + code checks + merge
# =============================================================================
def _sources_block(sources):
    blocks = []
    for i, s in enumerate(sources, 1):
        snippet = (s.get("snippet") or "")
        if s["origin"] != "rep_provided":
            snippet = snippet[:C.SNIPPET_CHARS]
        blocks.append(f"[S{i}] url: {s['url']}\ntitle: {s.get('title') or ''}\n"
                      f"published: {s.get('published_date') or 'unknown'}\ntext: {snippet}")
    return "\n\n".join(blocks)


_LEGAL = r"(?:inc|incorporated|ltd|limited|llc|llp|plc|corp|corporation|co|company|gmbh|ag|sa|pvt|private|pte)"


def company_core(company):
    """'Stripe, Inc.' -> 'Stripe'; 'Acme Pvt. Ltd.' -> 'Acme'."""
    core = (company or "").strip()
    while True:
        new = re.sub(rf"[\s,]+{_LEGAL}\.?$", "", core, flags=re.I).strip(" ,.")
        if new == core or not new:
            return core
        core = new


def mentions_company(text, company):
    core = company_core(company)
    return bool(core) and re.search(rf"(?<!\w){re.escape(core)}(?!\w)", text or "", re.I) is not None


def _mentions_person(text, name):
    t = (text or "").lower()
    n = name.lower().strip()
    if n in t:
        return True
    parts = [x for x in n.split() if len(x) > 1]
    return len(parts) >= 2 and parts[0] in t and parts[-1] in t


def extract_signals(p, sources):
    if not sources:
        return [], []
    sys = PR.EXTRACT_SYSTEM.format(types=", ".join(C.SIGNAL_TYPES), company=p["company"], name=p["name"])
    user = PR.EXTRACT_USER.format(name=p["name"], role=p["role"], company=p["company"],
                                  today=date.today().isoformat(), sources=_sources_block(sources))
    raw = call_llm(Extraction, sys, user).signals

    by_url = {norm_url(s["url"]): s for s in sources}
    kept, dropped = [], []
    for s in raw:
        sig = s.model_dump()
        src = by_url.get(norm_url(sig["source_url"]))
        if src is None:
            dropped.append({**sig, "drop_reason": "source_url was not gathered in Stage 3"})
            continue
        sig["source_url"] = src["url"]  # canonical form
        if parse_date(sig["date"]) is None:
            sig["date"] = None
        if sig["date"] is None and parse_date(src.get("published_date")):
            sig["date"] = parse_date(src["published_date"]).isoformat()
        text = f"{src.get('title') or ''} {src.get('snippet') or ''}"
        if (sig["level"] == "person" and src["origin"] != "rep_provided"
                and not (_mentions_person(text, p["name"]) and mentions_company(text, p["company"]))):
            dropped.append({**sig, "drop_reason": "person-level, but source does not mention both "
                                                  "the prospect's name and company (possible namesake)"})
            continue
        kept.append(sig)
    exclude = {w.lower() for w in re.findall(r"\w+", f"{p['name']} {p['company']}")}
    return merge_signals(kept, exclude), dropped


_GENERIC_CAPS = {"the", "a", "an", "ai", "crm", "ceo", "cto", "cfo", "coo", "vp", "inc", "ltd", "llc", "co",
                 "january", "february", "march", "april", "may", "june", "july", "august", "september",
                 "october", "november", "december", "series", "new", "its", "his", "her", "their"}


def key_entities(text, exclude):
    """Distinctive capitalised tokens (e.g. 'Warmly', 'Q3') minus the prospect/company names."""
    toks = re.findall(r"\b(?:[A-Z][a-zA-Z0-9]+|[A-Z]\d+)\b", text or "")
    return {t.lower() for t in toks} - _GENERIC_CAPS - exclude


def merge_signals(sigs, exclude=frozenset()):
    """Merge the same event reported by several outlets (heuristic): same type and
    level, dates within MERGE_MAX_DAYS (or both undated), and either overlapping
    summaries (Jaccard) or a shared distinctive entity such as the acquired company."""
    merged = []
    for s in sigs:
        d = parse_date(s["date"])
        s_ents = key_entities(s["summary"], exclude)
        for m in merged:
            mdates = m["_dates"]
            # An undated report is compatible with any date (outlets often omit it).
            same_time = d is None or not mdates or any(abs((d - x).days) <= C.MERGE_MAX_DAYS for x in mdates)
            shared = s_ents & m["_ents"]
            # A shared name identifies the event only for counterparty-type events (the acquired
            # company, the partner, the product); elsewhere require a shared period token like 'Q3'.
            entity_match = bool(shared) and (s["type"] in C.MERGE_ENTITY_TYPES
                                             or any(ch.isdigit() for t in shared for ch in t))
            similar = jaccard(m["summary"], s["summary"], exclude) >= C.MERGE_MIN_JACCARD or entity_match
            if m["type"] == s["type"] and m["level"] == s["level"] and same_time and similar:
                if s["source_url"] not in m["source_urls"]:
                    m["source_urls"].append(s["source_url"])
                if d:
                    mdates.append(d)
                    m["date"] = min(mdates).isoformat()  # earliest report = event date
                m["_ents"] |= s_ents
                break
        else:
            merged.append({k: v for k, v in s.items() if k != "source_url"}
                          | {"source_urls": [s["source_url"]], "_dates": [d] if d else [], "_ents": set(s_ents)})
    for m in merged:
        del m["_dates"], m["_ents"]
    return merged


# =============================================================================
# Stage 5 - Score & pick (LLM relevance, code for the rest)
# =============================================================================
def recency_T(d, today=None):
    d = parse_date(d)
    if d is None:
        return C.UNDATED_T
    days = ((today or date.today()) - d).days
    if days < -1:  # implausible future date: treat as undated
        return C.UNDATED_T
    for max_days, t in C.RECENCY_BANDS:
        if days <= max_days:
            return t
    return C.UNDATED_T


def score_signals(p, signals):
    if not signals:
        return []
    listing = "\n".join(f"[{i}] ({s['type']}, {s['level']}, date {s['date'] or 'unknown'}) "
                        f"{s['summary']} | evidence: \"{s['evidence']}\"" for i, s in enumerate(signals))
    sys = PR.RELEVANCE_SYSTEM.format(seller=seller_text(), name=p["name"], role=p["role"], company=p["company"])
    ratings = {r.signal_id: r for r in call_llm(Ratings, sys, PR.RELEVANCE_USER.format(signals=listing)).ratings}

    for i, s in enumerate(signals):
        r = ratings.get(i)
        s["R"] = max(0, min(3, int(r.R))) if r else 0
        s["R_reason"] = r.reason if r else "not rated by the LLM (treated as 0)"
        s["T"] = recency_T(s["date"])
        s["P"] = C.P_PERSON if s["level"] == "person" else C.P_COMPANY
        s["score"] = round((s["R"] / 3) * (C.W_RECENCY * s["T"] + C.W_PERSONAL * s["P"]), 3)
        s["passed_gate"] = int(s["R"] >= C.GATE_MIN_R)

    # Highest score; ties -> more recent, then person-level.
    def key(s):
        d = parse_date(s["date"])
        return (s["score"], d.toordinal() if d else 0, s["P"])
    ranked = sorted(signals, key=key, reverse=True)
    for rank, s in enumerate(ranked, 1):
        s["rank"] = rank
    return ranked


# =============================================================================
# Stage 6 - Draft (LLM)
# =============================================================================
def draft_email(p, sig, failures=None, previous=None):
    s = C.SELLER_PROFILE
    feedback = ""
    if failures:
        feedback = PR.DRAFT_FEEDBACK.format(failures="\n".join(f"- {f}" for f in failures),
                                            previous=json.dumps(previous, indent=2))
    sys = PR.DRAFT_SYSTEM.format(seller=seller_text(), company=p["company"], tone=s["tone"],
                                 subject_max=C.SUBJECT_MAX_WORDS, ctas=" | ".join(s["allowed_ctas"]),
                                 min_words=C.BODY_MIN_WORDS, max_words=C.BODY_MAX_WORDS,
                                 signal_type=sig["type"], framing=C.HOOK_FRAMING.get(sig["type"], C.HOOK_FRAMING["other"]))
    user = PR.DRAFT_USER.format(name=p["name"], role=p["role"], company=p["company"], signal_type=sig["type"],
                                summary=sig["summary"], evidence=sig["evidence"], date=sig["date"] or "unknown",
                                feedback=feedback)
    parts = call_llm(DraftParts, sys, user).model_dump()
    return parts, assemble(p, parts)


# =============================================================================
# Stage 7 - Check (code + one LLM grounding check)
# =============================================================================
def _norm_phrase(s):
    return " ".join(words(s))


def check_draft(p, sig, parts, body):
    checks = []
    core = core_text(parts)
    all_text = f"{parts['subject']} {core}"

    n_sub, n_body = word_count(parts["subject"]), word_count(core)
    checks.append({"check": "length",
                   "passed": n_sub <= C.SUBJECT_MAX_WORDS and C.BODY_MIN_WORDS <= n_body <= C.BODY_MAX_WORDS,
                   "detail": f"subject {n_sub} words (max {C.SUBJECT_MAX_WORDS}); body {n_body} words "
                             f"({C.BODY_MIN_WORDS}-{C.BODY_MAX_WORDS})"})

    low = all_text.lower()
    hits = [b for b in C.BANNED_PHRASES if b in low]
    checks.append({"check": "banned_phrases", "passed": not hits,
                   "detail": f"found: {', '.join(hits)}" if hits else "none found"})

    name_problems = []
    core_co = company_core(p["company"])
    if p["company"] not in all_text and not re.search(rf"(?<!\w){re.escape(core_co)}(?!\w)", all_text):
        name_problems.append(f"company not written exactly as '{p['company']}'")
    if re.match(r"\s*(hi|hello|dear|hey)\b", parts["hook"], re.I):
        name_problems.append("hook starts with its own greeting")
    first, last = p["name"].split()[0], p["name"].split()[-1]
    prose = " ".join(parts[k] for k in ("hook", "bridge", "value"))  # the CTA is fixed text
    # Mid-sentence capitalised words only (sentence-initial words like 'Open' are ordinary words).
    for tok in set(re.findall(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+\b", prose.strip())):
        # One letter off the prospect's first/last name is a likely misspelling.
        for real in {first, last}:
            if tok != real and len(real) > 3 and _edit1(tok.lower(), real.lower()):
                name_problems.append(f"'{tok}' looks like a misspelling of '{real}'")
    checks.append({"check": "names", "passed": not name_problems,
                   "detail": "; ".join(name_problems) or "company and prospect names match input"})

    allowed = {_norm_phrase(c) for c in C.SELLER_PROFILE["allowed_ctas"]}
    ask_problems = []
    if _norm_phrase(parts["cta"]) not in allowed:
        ask_problems.append(f"CTA is not one of the allowed CTAs: '{parts['cta']}'")
    pre = " ".join(parts[k] for k in ("hook", "bridge", "value"))
    if "?" in pre:
        ask_problems.append("a question appears outside the CTA (more than one ask)")
    if any(a and a in _norm_phrase(pre) for a in allowed):
        ask_problems.append("an allowed CTA also appears outside the CTA line")
    checks.append({"check": "one_ask", "passed": not ask_problems,
                   "detail": "; ".join(ask_problems) or "exactly one allowed CTA"})

    try:
        g = call_llm(Grounding, PR.GROUNDING_SYSTEM, PR.GROUNDING_USER.format(
            summary=sig["summary"], evidence=sig["evidence"], date=sig["date"] or "unknown",
            name=p["name"], role=p["role"], company=p["company"], seller=seller_text(),
            subject=parts["subject"], body=body))
        unsupported = [c.claim for c in g.claims if not c.supported]
        checks.append({"check": "grounding", "passed": not unsupported,
                       "detail": ("unsupported: " + " | ".join(unsupported)) if unsupported
                       else f"{len(g.claims)} claims, all supported",
                       "claims": [c.model_dump() for c in g.claims]})
    except StageError as e:
        # passed=None: the check could not run (infrastructure), which is not a failed check.
        checks.append({"check": "grounding", "passed": None, "detail": f"grounding check could not run: {e}"})
    return checks


def _edit1(a, b):
    """True if a and b differ by exactly one edit (insert, delete or substitute)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


# =============================================================================
# Stages 6 + 7 loop (shared by the pipeline and Regenerate)
# =============================================================================
def draft_and_check(run_id, p, sig, em, version, replaces=None):
    """Draft, check, redraft once on failure. Returns (draft_id, status).

    `replaces` is the draft id this generation replaces (Regenerate); it is
    marked superseded only once a new draft exists, so a failed regenerate
    leaves the old draft reviewable.
    Raises StageError only if the FIRST draft cannot be written. If a redraft
    errors, the previous (failed-check) draft is kept and flagged."""
    failures, previous, draft_id = None, None, None
    for attempt in range(1, C.MAX_DRAFT_ATTEMPTS + 1):
        label6 = f"Draft (v{version}, attempt {attempt})"
        em.start(6, label6)
        try:
            parts, body = draft_email(p, sig, failures, previous)
        except StageError as e:
            em.finish(6, "failed", str(e), label=label6)
            if draft_id is None:
                raise
            store.update_draft(draft_id, status="flagged")  # keep the attempt-1 draft for review
            return draft_id, "flagged"
        prior = draft_id or replaces
        draft_id = store.add_draft({"run_id": run_id, "signal_id": sig["id"], "version": version,
                                    "attempt": attempt, **parts, "body": body, "status": "checking"})
        if prior:
            store.update_draft(prior, status="superseded")
        em.finish(6, "done", f"\"{parts['subject']}\" - {word_count(core_text(parts))} words",
                  {"subject": parts["subject"], "body": body, "parts": parts}, label=label6)

        label7 = f"Check draft (v{version}, attempt {attempt})"
        em.start(7, label7)
        checks = check_draft(p, sig, parts, body)
        failed = [c for c in checks if c["passed"] is False]
        errored = [c for c in checks if c["passed"] is None]
        store.update_draft(draft_id, check_results=checks)
        if failed:
            summary = f"{len(failed)} failed: {', '.join(c['check'] for c in failed)}"
        elif errored:
            summary = f"{', '.join(c['check'] for c in errored)} could not run"
        else:
            summary = f"all {len(checks)} checks passed"
        em.finish(7, "warning" if failed or errored else "done", summary, {"checks": checks}, label=label7)
        if not failed and not errored:
            store.update_draft(draft_id, status="drafted")
            return draft_id, "drafted"
        if not failed:
            # A check could not run (e.g. LLM outage). Redrafting would not help; flag for the rep.
            break
        failures = [f"{c['check']}: {c['detail']}" for c in failed]
        previous = parts
    store.update_draft(draft_id, status="flagged")
    return draft_id, "flagged"


# =============================================================================
# Full pipeline
# =============================================================================
def run_pipeline(prospect, on_event=None):
    """Runs Stages 1-7. Returns the run id. Final status is one of
    drafted / flagged / needs_research / failed."""
    p = {k: (prospect.get(k) or "").strip() for k in ("name", "company", "role", "website", "notes")}
    missing = validate_input(p)
    if missing:
        raise ValueError(f"required fields missing: {', '.join(missing)}")

    t_run = time.time()
    run_id = store.create_run(p)
    em = Emitter(run_id, on_event)

    def end(status, error=None):
        store.update_run(run_id, status=status, duration=round(time.time() - t_run, 1), error=error)
        return run_id

    stage = 1
    try:
        em.start(1)
        em.finish(1, "done", f"{p['name']}, {p['role']} at {p['company']}",
                  {k: v for k, v in p.items() if v})

        stage = 2
        em.start(2)
        queries = plan_queries(p)
        em.finish(2, "done", f"{len(queries)} queries", {"queries": queries})

        stage = 3
        em.start(3)
        sources, counts, errors = gather_sources(queries, p["notes"])
        store.add_sources(run_id, sources)
        summary = f"{len(queries)} queries -> {len(sources)} sources"
        if errors:
            summary += f" ({len(errors)} queries failed)"
        em.finish(3, "done", summary, {**counts, "errors": errors, "sources": sources})

        stage = 4
        em.start(4)
        signals, dropped = extract_signals(p, sources)
        for s in dropped:
            s["source_urls"] = [s.pop("source_url")]
            s["verified"] = 0
            s["id"] = store.add_signal(run_id, s)
        for s in signals:
            s["verified"] = 1
            s["id"] = store.add_signal(run_id, s)
        em.finish(4, "done", f"{len(signals) + len(dropped)} signals extracted, {len(signals)} verified"
                             f"{f', {len(dropped)} dropped' if dropped else ''}",
                  {"verified": signals, "dropped": dropped})

        stage = 5
        em.start(5)
        ranked = score_signals(p, signals)
        for s in ranked:
            store.update_signal(s["id"], **{k: s[k] for k in ("R", "R_reason", "T", "P", "score", "passed_gate", "rank")})
        passing = [s for s in ranked if s["passed_gate"]]
        table = [{k: s[k] for k in ("id", "rank", "type", "level", "summary", "date", "R", "R_reason", "T", "P",
                                    "score", "passed_gate")} for s in ranked]
        if not passing:
            em.finish(5, "done", f"no signal with R >= {C.GATE_MIN_R} -> needs_research", {"ranked": table})
            return end("needs_research")
        chosen = passing[0]
        runner_ups = passing[1:1 + C.N_RUNNER_UPS]
        store.update_run(run_id, chosen_signal_id=chosen["id"])
        em.finish(5, "done", f"Hook: {chosen['type']} ({chosen['level']}), score {chosen['score']:.2f}",
                  {"ranked": table, "chosen_id": chosen["id"], "runner_up_ids": [s["id"] for s in runner_ups]})

        stage = 6
        _, status = draft_and_check(run_id, p, chosen, em, version=1)
        return end(status)

    except StageError as e:
        open_stages = em.fail_open(str(e))  # stage 6 closes its own event before raising
        return end("failed", f"stage {(open_stages or [stage])[-1]}: {e}")
    except Exception as e:  # unexpected bug: still close the run cleanly
        msg = f"{type(e).__name__}: {e}"
        open_stages = em.fail_open(msg)
        return end("failed", f"stage {(open_stages or [stage])[-1]}: {msg}")
    except BaseException:
        # Interrupted (Ctrl-C, or Streamlit rerun/stop when the page is touched mid-run).
        # Never leave the run stuck in 'running'.
        em.fail_open("interrupted")
        end("failed", "interrupted before finishing")
        raise


# =============================================================================
# Stage 8 - Review actions
# =============================================================================
def runner_ups(run_id):
    """The chosen hook plus the N_RUNNER_UPS kept at Stage 5, best first."""
    return [s for s in store.get_signals(run_id) if s["passed_gate"]][:1 + C.N_RUNNER_UPS]


def _reviewable(run):
    if run["status"] not in ("drafted", "flagged"):
        raise ValueError(f"run {run['id']} is '{run['status']}', not reviewable")


def approve(run_id, subject=None, body=None):
    run = store.get_run(run_id)
    _reviewable(run)
    d = store.current_draft(run_id)
    if subject is not None or body is not None:
        save_edit(run_id, subject, body)
        d = store.get_draft(d["id"])
    store.update_draft(d["id"], status="approved")
    store.update_run(run_id, status="approved")
    store.add_review_action(run_id, d["id"], "approve")
    return final_text(d)


def save_edit(run_id, subject, body):
    run = store.get_run(run_id)
    _reviewable(run)
    if not (subject or "").strip() or not (body or "").strip():
        raise ValueError("subject and body cannot be empty")
    d = store.current_draft(run_id)
    cur_subject = _pick(d["edited_subject"], d["subject"])
    cur_body = _pick(d["edited_body"], d["body"])
    if subject == cur_subject and body == cur_body:
        return False
    store.update_draft(d["id"], edited_subject=subject, edited_body=body)
    store.add_review_action(run_id, d["id"], "edit")
    return True


def discard(run_id, reason):
    run = store.get_run(run_id)
    _reviewable(run)
    d = store.current_draft(run_id)
    store.update_draft(d["id"], status="discarded")
    store.update_run(run_id, status="discarded")
    store.add_review_action(run_id, d["id"], "discard", reason)


def regenerate(run_id, signal_id=None, on_event=None):
    """Redraft from the same signal, or from a runner-up the rep picks.
    Returns (status, error). If drafting fails, the old draft stays current and
    the run keeps its previous status, so the rep can still act on it."""
    run = store.get_run(run_id)
    _reviewable(run)
    old = store.current_draft(run_id)
    sig = store.get_signal(signal_id or run["chosen_signal_id"])
    allowed = {s["id"] for s in runner_ups(run_id)}
    if sig is None or sig["id"] not in allowed:
        raise ValueError("signal must be the chosen hook or one of its runner-ups")
    p = {k: run[k] for k in ("name", "company", "role", "website", "notes")}
    store.add_review_action(run_id, old["id"], "regenerate", f"signal {sig['id']}")
    version = max(d["version"] for d in store.get_drafts(run_id)) + 1
    store.update_run(run_id, status="running")
    em = Emitter(run_id, on_event)
    t = time.time()

    def restore(err):
        em.fail_open(err)
        store.update_run(run_id, status=run["status"], chosen_signal_id=run["chosen_signal_id"],
                         error=f"regenerate failed: {err}")
        store.add_review_action(run_id, old["id"], "regenerate_failed", err)

    try:
        _, status = draft_and_check(run_id, p, sig, em, version, replaces=old["id"])
    except StageError as e:
        restore(str(e))
        return run["status"], str(e)
    except Exception as e:
        restore(f"{type(e).__name__}: {e}")
        return run["status"], f"{type(e).__name__}: {e}"
    except BaseException:
        cur = store.current_draft(run_id)
        if cur and cur["id"] != old["id"]:  # a new draft was written before the interrupt
            store.update_draft(cur["id"], status="flagged")
            store.update_run(run_id, status="flagged", chosen_signal_id=sig["id"])
            em.fail_open("interrupted")
        else:
            restore("interrupted")
        raise
    store.update_run(run_id, status=status, chosen_signal_id=sig["id"], error=None,
                     duration=round((run["duration"] or 0) + time.time() - t, 1))
    return status, None


def _pick(edited, original):
    return edited if edited is not None else original


def final_text(d):
    return f"Subject: {_pick(d['edited_subject'], d['subject'])}\n\n{_pick(d['edited_body'], d['body'])}"


# =============================================================================
# CLI
# =============================================================================
def _print_event(ev):
    if ev["status"] == "started":
        print(f"[{ev['stage']}] {ev['stage_name']} ...", flush=True)
    else:
        print(f"[{ev['stage']}] {ev['stage_name']}: {ev['status'].upper()} - {ev['summary']} "
              f"({ev['elapsed']}s)", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the outreach pipeline for one prospect.")
    ap.add_argument("name")
    ap.add_argument("company")
    ap.add_argument("role")
    ap.add_argument("--website", default="")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    rid = run_pipeline(vars(a), on_event=_print_event)
    run = store.get_run(rid)
    print(f"\nRun {rid}: {run['status']}" + (f" ({run['error']})" if run["error"] else ""))
    for s in store.get_signals(rid):
        if s["verified"]:
            print(f"  #{s['rank']} score={s['score']} R={s['R']} T={s['T']} P={s['P']} "
                  f"[{s['type']}/{s['level']}] {s['summary']}")
    d = store.current_draft(rid)
    if d:
        print("\n" + final_text(d))
        for c in d["check_results"] or []:
            print(f"  {'PASS' if c['passed'] else 'FAIL'} {c['check']}: {c['detail']}")
