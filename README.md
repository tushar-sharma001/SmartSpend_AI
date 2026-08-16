# SmartSpend AI

An AI-powered personal expense tracker with per-user accounts, built with
Streamlit, SQLite, and Google Gemini.

Register → log in → log an expense in plain English → Gemini auto-
categorizes it and flags anything unusually large for you → ask questions
about your spending in plain English → chat with a budgeting assistant
that only ever sees your own data.

## Features

- **Accounts** — register/login with hashed passwords (Werkzeug), session
  managed via Streamlit's `session_state`.
- **AI expense categorization** — Gemini classifies each expense into a
  fixed category set, with a keyword-based fallback if the API is
  unavailable.
- **Anomaly detection** — flags expenses that are statistically unusual
  for you, based on your own spending history.
- **Ask AI** — ask questions about your spending in plain English; the
  question is converted to SQL, validated, and run against your data only.
- **AI Financial Assistant** — a conversational chatbot for budgeting
  questions, grounded only in a summary of your own data, with built-in
  guardrails against investment/tax/legal advice.
- **Budgets & notifications** — set monthly limits per category and get
  warnings as you approach or exceed them.
- **Dashboard & Analytics** — spending by category, monthly trends, top
  expenses, and deterministic insights (month-over-month comparisons).
- **Transactions** — search, filter, sort, and export to CSV.
- **Light/dark theme**, empty states, and a card-based UI via custom CSS.

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| Language | Python 3.12 |
| Database | SQLite |
| AI / LLM | Google Gemini (`gemini-3.1-flash-lite`) |
| Auth | Werkzeug (password hashing) |
| SQL validation | sqlparse |
| Data / charts | Pandas, Plotly |
| Testing | pytest |
| Deployment | Docker, Streamlit Community Cloud |
| CI | GitHub Actions |

## Project Structure

```
smartspend-ai-pro/
├── app.py            # Streamlit UI
├── src/
│   ├── config.py       # Config & secrets
│   ├── database.py     # SQL & user-scoped queries
│   ├── auth.py          # Login/register
│   ├── ai_service.py    # Gemini calls + fallback
│   ├── sql_validator.py # Ask AI safety checks
│   ├── chatbot.py       # Chat orchestration
│   └── insights.py      # Notifications & insights
├── tests/             # pytest suite
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Architecture

```
Streamlit UI
  |
  |-- Auth --> src.auth --> session_state.user
  |
  |-- Add Expense --> src.ai_service (categorize + detect anomaly) --> src.database
  |
  |-- Ask AI --> Gemini generates SQL --> src.sql_validator --> src.database (scoped) --> Gemini summarizes
  |
  |-- AI Assistant --> src.chatbot --> src.ai_service --> src.database
```

## Authentication

Passwords are hashed with Werkzeug (`generate_password_hash`) and never
stored in plaintext. Login returns a generic "Invalid email or password"
on any failure, so an attempt can't reveal which emails are registered.
A per-email login throttle (5 failed attempts / 5 minutes) blocks further
attempts, including with the correct password, until the window clears.
Sessions live in Streamlit's `session_state` for the duration of the
browser session.

## Data Isolation & SQL Security

Every expense, budget, and chat message is scoped to a `user_id`, and
every query is filtered accordingly — covered by the isolation tests in
`tests/test_database.py`.

Ask AI adds two layers on top of that:

1. **Statement-level validation** (`src/sql_validator.py`) — Gemini's
   generated SQL is tokenized with `sqlparse` and must be a single
   `SELECT` statement against an allowed table, with no write/DDL
   keywords. A row limit is enforced automatically.
2. **Data-level isolation** (`src/database.run_scoped_readonly_query`) —
   the validated query runs against an in-memory copy containing only the
   current user's rows, so it physically cannot return another user's
   data regardless of the query itself.

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env
# add GEMINI_API_KEY — free at https://aistudio.google.com/apikey
streamlit run app.py
```
Opens at `http://localhost:8501`. Register an account on first run.

### Secrets

For local development, `.env` is all you need. For Streamlit Community
Cloud, use the app's Settings → Secrets dashboard (or a local
`.streamlit/secrets.toml`, see `.streamlit/secrets.toml.example`) with the
same key names — `src/config.py` checks Streamlit secrets first and falls
back to environment variables automatically.

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Run it in Docker

```bash
docker build -t smartspend-ai .
docker run -p 8501:8501 --env-file .env -v smartspend_data:/app/data smartspend-ai
```

## Known Limitations

- Session storage uses Streamlit's `session_state`, not a signed cookie
  or server-side session store.
- The login throttle is in-memory and per-process, so it resets on
  restart and won't coordinate across multiple instances.
- The SQL validator checks statement type and table names, not individual
  columns — data-level isolation is the primary safeguard for Ask AI.
- Anomaly detection is a statistical threshold (mean + 2×std-dev with
  ≥3 prior expenses in a category), not a trained model.
- No password reset, email verification, or OAuth.

## Future Improvements

- **Server-side session store** — replace `session_state` with a signed
  cookie or a proper session store so auth survives a server restart and
  works across multiple instances.
- **Shared login throttle** — move the in-memory throttle to a shared
  store (e.g. Redis) so it holds up under multiple worker processes.
- **Column-level SQL validation** — extend `sql_validator.py` beyond
  statement/table checks to verify referenced columns too, for a second
  layer of defense on top of data-level isolation.
- **Password reset & email verification** — currently out of scope; would
  need an email-sending service.
- **OAuth login** (Google/GitHub) as an alternative to email/password.
- **Recurring expenses** — support for subscriptions and other repeating
  transactions instead of one-off entries only.
- **Multi-currency support** — currently assumes a single currency (₹)
  throughout.
- **Richer anomaly detection** — once enough historical data exists,
  revisit whether a lightweight learned model outperforms the current
  statistical threshold.
