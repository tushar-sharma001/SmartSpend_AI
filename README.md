# SmartSpend AI

An AI-powered personal expense tracker with per-user accounts, built with
production practices on top of a deliberately small stack: **pure
Streamlit** (no separate frontend, no Flask, no HTML/JS to maintain
alongside it), SQLite, and Gemini.

Register → log in → log an expense in plain English → Gemini
auto-categorizes it and flags anything unusually large for you specifically
→ ask questions about your spending in plain English → chat with a
budgeting assistant that only ever sees your own data.

## Why Streamlit, not Flask + HTML/JS

An earlier draft of this spec considered a full rewrite into a separate
Flask backend with a hand-built HTML/CSS/JS frontend. That was deliberately
**not** done: it would mean learning and defending a second framework
(Flask sessions, `fetch()`-based API calls) on top of everything else,
with very little runway before presenting this. Streamlit already renders
real HTML/CSS under the hood, so the modern-SaaS look is achieved by
injecting custom CSS into Streamlit's own output — same visual result,
one framework, one thing to explain.

## What's new since the single-user version

- **Accounts.** Register/login, Werkzeug password hashing, session held in
  Streamlit's own `session_state` — no JWTs, no OAuth, no external auth
  service.
- **Every table is user-scoped.** Expenses, budgets, and chat history all
  carry a `user_id`, and every query is scoped to the logged-in user — see
  `tests/test_database.py`'s isolation tests.
- **A second, harder AI feature: the AI Financial Assistant.** A
  conversational chatbot (`src/chatbot.py`) distinct from Ask AI — it
  answers from a short, deterministic summary of your data (never sends
  the raw database to Gemini) and refuses out-of-scope questions (stocks,
  tax, loans, legal) before even calling the model.
- **A hardened SQL validator** (`src/sql_validator.py`) using `sqlparse`
  (a real tokenizer) instead of a keyword/regex scan: single-statement
  only, SELECT-only, table allowlist, automatic row-limit enforcement.
- **Data-level isolation on top of SQL validation.** Ask-AI questions never
  run against the real database at all — `database.run_scoped_readonly_query`
  builds a throwaway in-memory copy containing *only* the logged-in user's
  rows and runs the (validated) query against that. Even a query that
  somehow slipped past the validator still can't see another user's data,
  because that data was never copied into the scratch database.
- **Deterministic insights and notifications** (`src/insights.py`) — month-
  over-month category comparisons, budget-threshold warnings, anomaly
  alerts. No Gemini calls for arithmetic Python already does correctly.
- **Transactions page** with search, category filter, sort, and CSV export.
- **Light/dark theme toggle**, empty states, and a modern card-based layout
  via injected CSS.

## Project structure

```
smartspend-ai-pro/
├── app.py                    # Streamlit UI — pages, widgets, auth gate
├── src/
│   ├── config.py              # Centralized env/config loading + logger factory
│   ├── database.py            # All SQL; user-scoped CRUD; scoped-copy query execution
│   ├── auth.py                # Registration/login validation, hashing, throttle
│   ├── ai_service.py          # Gemini calls (categorize, Ask-AI SQL gen, chat), keyword fallback
│   ├── sql_validator.py       # Tokenizer-based SELECT-only/table-allowlist validator
│   ├── chatbot.py             # Chat orchestration, context building, off-topic redirect, history
│   └── insights.py            # Deterministic notifications + insights (no LLM calls)
├── tests/                     # 67 tests — auth, isolation, SQL safety, anomaly detection
├── .github/workflows/ci.yml   # Runs the test suite on every push
├── Dockerfile
├── .streamlit/config.toml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env
# add GEMINI_API_KEY — free at https://aistudio.google.com/apikey
# leave it blank to run in offline/demo mode: registration/login/expenses/
# budgets/transactions all still work; categorization falls back to
# keyword matching; Ask AI and AI Assistant are disabled.
streamlit run app.py
```
Opens at `http://localhost:8501`. Register an account on first run — this
is a fresh database with no pre-seeded users.

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```
67 tests, no network calls, runs in a few seconds. Verified in a clean
virtualenv against nothing but `requirements-dev.txt` before this was
handed over.

## Run it in Docker

```bash
docker build -t smartspend-ai .
docker run -p 8501:8501 --env-file .env -v smartspend_data:/app/data smartspend-ai
```

## Architecture — how a request actually flows

```
Streamlit UI (app.py)
      |
      |-- not logged in --> src.auth (register/authenticate) --> session_state.user
      |
      |-- Add Expense --> src.ai_service.categorize() --> Gemini (or keyword fallback)
      |                --> src.ai_service.detect_anomaly() --> pure statistics, this user's history only
      |                --> src.database.insert_expense(user_id, ...)
      |
      |-- Ask AI --> src.ai_service.answer_question(user_id, question)
      |                --> Gemini generates SQL
      |                --> src.sql_validator.validate_and_prepare()   [statement-level safety]
      |                --> src.database.run_scoped_readonly_query()   [data-level isolation]
      |                --> Gemini summarizes the result
      |
      |-- AI Assistant --> src.chatbot.send_message(user_id, message)
                       --> off-topic keyword check (no Gemini call if matched)
                       --> src.chatbot.build_context(user_id)  [short summary, never raw rows]
                       --> src.ai_service.generate_chat_reply()
                       --> src.database.insert_chat_message() (both turns, scoped to user)
```

## Authentication — how it actually works

Registration validates name/email/password client-and-server-side, hashes
the password with Werkzeug's `generate_password_hash` (scrypt-based), and
stores it — the plaintext password is never written anywhere. Login
verifies the hash and returns a generic "Invalid email or password" on
*any* failure (wrong password or unknown email alike), so a login attempt
can't be used to check which emails are registered. A simple in-memory
per-email throttle (5 failed attempts / 5 minutes) blocks further login
attempts, including ones with the correct password, until the window
clears.

The session itself lives in Streamlit's `session_state` for the duration
of that browser tab/session — there is no server-side session store, no
JWT, no cookie signing. That's a real, honest simplification: it's
appropriate for a single-process local/demo deployment and would be the
first thing to replace (with a proper server-side session store) before
this ever ran multi-instance or at real scale.

## Text-to-SQL security — the two independent layers

1. **Statement-level (`src/sql_validator.py`):** the LLM's generated SQL is
   tokenized with `sqlparse` and must be exactly one `SELECT` statement,
   contain no forbidden keyword (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/
   `PRAGMA`/`ATTACH`/etc.), and reference only `expenses` or `budgets`. A
   `LIMIT` is enforced (appended if absent, capped at 100 if too large).
2. **Data-level (`src/database.run_scoped_readonly_query`):** the validated
   query never touches the real database. It runs against a throwaway
   in-memory SQLite copy containing *only* the current user's rows, built
   fresh per request from a read-only connection to the real database.
   Even a query that somehow passed validation but omitted a user filter
   still can't see another user's data — that data was never copied in.

Both layers are deliberately not a full SQL parser/AST with column-level
whitelisting — see "Known Limitations" below for the honest boundary.

## Testing

67 tests across 4 files:
- `test_database.py` — CRUD, constraints, and **user isolation** across
  expenses, budgets, chat messages, and the scoped query function.
- `test_auth.py` — registration validation, duplicate-email rejection,
  password hashing, correct/incorrect login, the generic error message,
  and the login throttle (including that it blocks even a *correct*
  password once tripped, and resets after a successful login).
- `test_sql_validator.py` — every forbidden statement type, multi-statement
  injection attempts, unauthorized table references, and LIMIT enforcement.
- `test_ai_service.py` — keyword categorization fallback, anomaly detection
  edge cases (including per-user scoping), and graceful offline fallbacks.

## Known Limitations (say these proactively — they show maturity)

- **Session storage is Streamlit's own `session_state`**, not a signed
  cookie or server-side session store. Fine for a single-process local
  deployment; the first thing to change for anything running multiple
  server instances.
- **The login throttle is in-memory and single-process** — resets on
  restart, doesn't coordinate across multiple workers. A real deployment
  would move this to a shared store (Redis, etc.).
- **The SQL validator is table-level, not column-level.** It confirms the
  query only touches `expenses`/`budgets` and is a single safe `SELECT`,
  but doesn't verify every referenced column exists or is appropriate —
  full column-level SQL parsing for arbitrary generated queries is a much
  larger problem than this app's threat model justifies, given the
  data-level isolation layer underneath it.
- **The anomaly detector is a statistical threshold** (mean + 2×std-dev
  once ≥3 prior expenses exist), not a trained model — deliberately, since
  there's no labeled dataset of "actually anomalous" expenses to train on.
- **The chatbot has no long-term memory beyond the stored transcript** —
  each reply is generated from a short summary + the last 20 messages, not
  a retrieval system over the user's full history.
- **No password reset, no email verification, no OAuth** — by design, to
  keep auth "mid-level and easy to explain" rather than adding
  infrastructure (email sending, third-party auth providers) this project
  doesn't need to make its point.
