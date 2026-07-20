# Contributing to JobHuntWOW (app)

## Dev setup
```bash
cp .env.example .env            # add DO_INFERENCE_KEY
# backend
cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload   # :8000
# frontend
cd frontend && npm install && npm run dev                                        # :5173 (proxies /api)
```

## Ground rules
- **KISS + zero manual entry** — that's the product. New features should reduce user effort.
- **Never commit secrets.** `.env` is gitignored; put placeholders in `.env.example`.
- **Keep the `/api/*` contract stable** — the frontend depends on it. If you must change a shape,
  update `frontend/src/api.js` and the page in the same PR.
- **Render safely** — coerce unknown backend values to text before putting them in JSX.
- **LLM never triggers side effects.** Qwen writes prose; explicit user actions trigger apply/store/send.

## Where things go
| Change | File |
|--------|------|
| New API route | `backend/app/main.py` (+ `store.py`/`qwen.py`/`scout.py` as needed) |
| Real job scout | `backend/app/scout.py::search()` (keep `{query,count,jobs,note}`) |
| ATS apply logic | `POST /api/apply` handler (must require an explicit user action) |
| New page | `frontend/src/pages/*.jsx` + a route in `App.jsx` + a nav item |
| Styling | `frontend/src/styles.css` |

## Commit / PR
- Small, focused commits; imperative subject ("Add scout pagination").
- Run `docker compose up --build` and click through Dashboard / Hermes / Scout / Pipeline before opening a PR.
