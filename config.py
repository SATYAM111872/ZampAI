"""Static configuration: seller profile (PLAN.md section 4), query templates,
scoring weights, check rules and timeouts. Edit this file, not agent.py."""

# ---------------------------------------------------------------------------
# Seller profile (assumption A1: one seller, set once).
# EXAMPLE CONTENT - replace with the real seller before demoing.
# proof_points is intentionally empty: the draft may cite anything listed here,
# so only add facts you can stand behind.
# ---------------------------------------------------------------------------
SELLER_PROFILE = {
    "product": "Clearbook",
    "one_liner": "AI agents that automate accounts-payable work for finance teams: "
                 "invoice intake, PO matching, approvals and vendor queries.",
    "function": "finance",  # used by the Stage 2 hiring query
    "buyers": ["CFO", "VP Finance", "Controller", "Head of Finance Operations",
               "Head of Accounts Payable"],
    "problems_solved": [
        "Manual invoice entry and three-way PO matching that does not scale with volume",
        "Finance teams that must grow headcount every time the company grows",
        "Slow month-end close caused by unreconciled payables",
        "Vendor emails and payment-status queries eating AP team time",
        "Integrating finance operations after an acquisition or expansion into new entities",
    ],
    "proof_points": [],
    "allowed_ctas": [
        "Worth a 15-minute call next week?",
        "Want me to send over a one-pager?",
        "Open to a short demo?",
    ],
    "rep_name": "Alex",
    "sign_off": "Best,\nAlex",
    "tone": "Direct, plain, no fluff.",
}

# ---------------------------------------------------------------------------
# Stage 2 - query templates
# ---------------------------------------------------------------------------
COMPANY_QUERIES = [
    '"{company}" funding',
    '"{company}" news',
    '"{company}" hiring {function}',
    '"{company}" launch OR expansion OR acquisition',
]
PERSON_QUERIES = [
    '"{name}" {company} interview OR podcast',
    '"{name}" {company}',
    '"{name}" {company} appointed OR joins OR promoted',
]
WEBSITE_QUERY = "site:{domain} news OR announcement OR blog"

# ---------------------------------------------------------------------------
# Stage 3 - gathering
# ---------------------------------------------------------------------------
RESULTS_PER_QUERY = 5
MAX_SOURCES = 30
SEARCH_TIMEOUT_S = 20
SEARCH_WORKERS = 4          # parallel queries; higher values get rate-limited
SNIPPET_CHARS = 500         # per source, sent to the LLM

# ---------------------------------------------------------------------------
# Stage 4 - signals
# ---------------------------------------------------------------------------
SIGNAL_TYPES = ["funding", "hiring", "leadership_change", "product_launch", "expansion",
                "acquisition", "partnership", "exec_statement", "other"]
MERGE_MAX_DAYS = 7          # same event if dates within this many days
MERGE_MIN_JACCARD = 0.35    # ...and summaries overlap this much,
MERGE_ENTITY_TYPES = {"acquisition", "partnership", "funding", "product_launch"}  # ...or share a name

# ---------------------------------------------------------------------------
# Stage 5 - scoring. Starting defaults, not values from a study; tune them
# against the hand-labelled test set (PLAN.md section 7).
# ---------------------------------------------------------------------------
W_RECENCY = 0.6
W_PERSONAL = 0.4
RECENCY_BANDS = [(14, 1.0), (60, 0.7), (180, 0.4)]  # (max_days, T); else UNDATED_T
UNDATED_T = 0.1
P_PERSON = 1.0
P_COMPANY = 0.6
GATE_MIN_R = 2
N_RUNNER_UPS = 2

# ---------------------------------------------------------------------------
# Stage 6/7 - draft and checks
# ---------------------------------------------------------------------------
BODY_MIN_WORDS = 60
BODY_MAX_WORDS = 120
SUBJECT_MAX_WORDS = 8
MAX_DRAFT_ATTEMPTS = 2      # first draft + one redraft, then flagged

BANNED_PHRASES = [
    "hope this finds you well", "hope this email finds you", "hope you're doing well",
    "i wanted to reach out", "just reaching out", "just checking in", "touch base",
    "circle back", "quick question", "i came across your profile", "synergy",
    "game-changer", "game changer", "revolutionize", "revolutionise", "cutting-edge",
    "best-in-class", "world-class", "leverage", "low-hanging fruit", "no-brainer",
]

HOOK_FRAMING = {
    "funding": "Saw {company} just closed {round}; that usually means pressure to {relevant outcome}...",
    "hiring": "Noticed you're hiring for {role}; that's often a sign {pain} is a priority...",
    "exec_statement": "Read your comments on {topic} in {outlet}: {short paraphrase}. That's the problem we work on...",
    "leadership_change": "Congrats on the move to {role}. The first months usually mean {pressure}...",
    "product_launch": "Saw the {launch}. Scaling that usually means {pain}...",
    "expansion": "Saw the {expansion}. Scaling that usually means {pain}...",
    "acquisition": "With {event}, {integration pain} tends to show up next...",
    "partnership": "With {event}, {integration pain} tends to show up next...",
    "other": "State the event specifically, then connect it to one problem the seller solves.",
}

# ---------------------------------------------------------------------------
# Timeouts / storage
# ---------------------------------------------------------------------------
LLM_TIMEOUT_S = 90               # per call, including retries
LLM_RETRY_BACKOFF_S = [3, 8]     # waits before retrying a 429/503
DB_PATH = "data/runs.db"
