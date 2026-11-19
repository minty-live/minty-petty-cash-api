# Expense AI - reading a receipt into the expense form

When a receipt is attached on minty-petty-cash-web's Expenses stage and the person presses **Read
receipt** (never on attaching it - owner, 2026-11-18), a vision model reads it and the form's fields (amount, date, description, supplier, account) are **pre-filled as suggestions** the
person confirms. Nothing is saved without their **Add**, a supplier is never created automatically,
and the form stays editable while the read runs (or fails) in the background. The product and
technical plans are Minty's `docs/expense_ai/Stage 1 *.md` (history; their section numbers are the
ones the code cites).

**Moved here from Flask on 2026-11-04 (owner).** Until then this service forwarded the receipt to
Flask's `POST /api/internal/petty-cash/expense-extract`; now it reads it itself, on the engine it
shares with minty-payment-request-api's invoice reader. The answer to the web is unchanged.

## Where it lives

- `GET /reports/expenses` carries `reader` (`EXPENSE_AI_ENABLED` and a usable `ai_policy`, read on
  the call, with the company's AI allowance left - below): the app offers "Read receipt" only
  when it is on. Switched off it is `{"enabled": false}` alone.
- `POST /reports/expenses/extract` (`petty_cash/routes/expenses.py::extract_expense`) - who may
  ask (`require_edit`, 403) and the file (one PDF / JPEG / PNG of at most 10 MB by its bytes,
  `services/expenses.py::checked_receipt`, 422) are refused with the Expenses step's sentences.
  Past those every outcome is 200 `{suggestions, reason, reader}`; `suggestions` is null on any
  failure (and on `limit_reached`) and the app shows nothing.
- `petty_cash/services/receipt_reader.py` - the receipt's half: the instruction, the reply's shape
  (`ExpenseSuggestion`), the company's lists sent with it (`build_context`: the same
  `expense_accounts` and `suppliers` the Expenses step offers, so nothing is suggested that cannot
  be chosen) and the check of the reply against those lists (`validate`: an id is accepted only if
  it was sent; a value that fails a check is dropped, never repaired).
- `minty_shared.ai` (minty-shared-py, extra `ai`) - the engine: the switches, the models and their
  failover, the call and its one retry, usage and cost, and the ONE log line (numbers only, never a
  value read off a receipt; `model=` is the model that answered, `attempts=` the chain). `minty_shared.model_input` makes the small copy the model reads (an
  image capped at 2048 px and saved once as JPEG; a PDF's first page). The stored receipt is never
  changed: it reaches B2 exactly as uploaded.
- `python manage.py expense_ai_check` - one round-trip call with a generated image, from wherever
  the code runs, to prove the configuration (the policy's models, which one answered).
- `python manage.py ai_policy` - shows the policy and each company's reads; `--entity <id>` one
  company's grants and reads; `--grant <id> --tokens n [--reads n] [--days n] [--note ...]` gives a
  grant; `--set-models PRIMARY [BACKUP]`, `--no-backup`, `--set-default-grant '<json>|null'`,
  `--set-rules '<json>'` change the policy after checking it (an unapproved provider, an unusable
  rule or a grant of nothing is refused).
- The screen is minty-petty-cash-web's: it marks a suggested value visibly, clears it with one tap
  and never overwrites what the person already typed.

## The company's AI allowance and the models (owner, 2026-11-18)

AI reading is a preview. **Each company reads against its own grants**, Petty Cash and Payment
Request together, and the models are settings - all in minty-db (v0.13), changed with an UPDATE or
minty-petty-cash-api's `python manage.py ai_policy`, never a release:

- `ai_grant`: an allowance given to one company - `tokens_total` and/or `reads_total`, an optional
  `expires_at`, `source` (`default` | `manual`), who gave it and why. A top-up is another grant. A
  used-up or expired grant is spent and stays as the record.
- `ai_policy` (one row): `primary_model`, `backup_model`, `default_grant` - the one-time grant every
  company is given automatically on its first read (`{"tokens": n, "reads": n, "days": n}`; NULL =
  only grants given by hand) - and `limit_rules`, optional extra caps over every company
  (`{"reads": n, "tokens": n, "period_days": n}`, all applying; a rolling window, e.g. a daily
  throttle). The preview: `default_grant {"tokens": 20000}` (about four to five receipts; a read is
  ~3,500-5,700 tokens), no extra caps. The default grant is given once per company, ever
  (`uq_ai_grant_one_default`): a used-up company stays used up until it is given another grant
  (`manage.py ai_policy --grant <company id> --tokens 50000 [--reads n] [--days n] [--note ...]`,
  or minty-web's Select Company in its **AI allowances** mode - every superuser; minty-accounts-api's
  `docs/features/ai-allowances.md`).
- `ai_read`: one row per read asked for - `pending`, then `ok` or `failed` - charged to one grant
  (the oldest with room), with the module, the model that answered, the chain tried
  (`gemini-3.5-flash-lite:timeout>gemini-3.8-flash:ok`) and every attempt's tokens.
- **What counts:** a grant is charged the tokens of reads a model answered (even with nothing usable
  in them); a failed read uses none of it, though its billed tokens are recorded and count against
  the extra caps (our cost guard). A grant is used up once its tokens reach its total - the read
  that crosses it finishes. A refusal before the model is asked (switched off, file too large, no
  accounts or suppliers) uses nothing.
- **How:** `minty_shared.django.ai_allowance` (minty-shared-py v0.7) - `reserve` under a per-company
  Postgres advisory lock (it gives the default grant on the first read; two clicks with room for
  one: one passes), `finish`, `give_grant`, `reader_status`. A missing or unusable policy row turns
  reading OFF and logs an error - never a guessed limit.
- **Failover:** the engine tries `primary_model`, then `backup_model` when the first times out,
  errors, is rate-limited, truncates or replies unusably (not on a safety block). Only a provider in
  `minty_shared.ai.APPROVED_PROVIDERS` (Gemini today) is ever called; a model id names its provider
  (`gemini-...`, or `<provider>:<model>`), so another approved provider can be added later.
- **The answer** carries `reader` - `{enabled, percent_left, reads_used, reads_limit, reads_left,
  blocked, resumes_at}` - after every read, so the page's "80% of your AI allowance left" moves
  with no extra call. `percent_left` is measured over the grants that still have room (a top-up
  after a spent grant reads 100%). Used up, the route answers `limit_reached` without asking the
  model and the page disables the button.
- The in-process `RateLimiter` and `*_RATE_USER_PER_MIN`/`*_RATE_ENTITY_PER_HOUR` are gone (replaced).
- **Production:** the owner chose Vertex AI (2026-11-18); until it is set up production still uses
  the direct Gemini API on the FREE tier, which does not meet "approved providers only".

## The prompt update (owner, 2026-11-19)

Three rules join the instruction, before its last line:

- **Currency:** `currency` is the ISO 4217 code of the amounts on the document; when only "$" is
  printed, the model infers it from the issuer's country (an HKD company's "$" receipt from a US
  shop comes back USD and is then converted, above).
- **The date as printed:** `date_raw`, character for character, never reformatted. Handed on in
  the answer as untrusted text (stripped, capped at 40) and never logged; no screen uses it yet -
  it is there for checking an ambiguous day such as 01/02.
- **A card slip alone:** `card_slip_only` is true when the document is only a card terminal slip
  that does not show what was bought (the model also leaves `description_value` and `account_id`
  empty). The route then answers `{suggestions: null, reason: "card_slip_only"}` - nothing is
  filled and no rate is asked - and the page says "A card slip alone is not valid evidence. Please
  attach a receipt that shows what was bought." The read still counts against the company's
  allowance: a model answered.

## A receipt in another currency (owner, 2026-11-19)

When the receipt's currency is not the company's, the amount comes back **converted** at
Frankfurter's rate (https://frankfurter.dev, v1: the European Central Bank's reference rates, 30
currencies, no key) - `minty_shared.fx` (minty-shared-py v0.8), after the read and outside the
models' failover, so one call per read:

- **The day:** the receipt's `document_date` (basis `receipt`); the report's day instead
  (`report_date`) when the receipt's is empty, unreadable, in the future or more than a year from it.
  The extract route takes the report's day as `transaction_date` (optional; absent = the company's
  today, a non-date = 422). A weekend or holiday gets the previous business day's rate, and the day
  Frankfurter names is the one kept.
- **The amount:** original x rate, half up, 2 places; the 10,000,000 cap applies to the converted
  amount (a receipt in another currency is taken as read up to 999,999,999,999). The answer carries
  `exchange` - `{original_currency, original_amount, rate, rate_date, date_basis, source}`.
- **No rate** (any error, a timeout of 5 s, a currency v1 doesn't have such as TWD): the amount
  comes back empty, `exchange_failed: {currency}`, and the person types it. Nothing is blocked.
- **The line keeps it:** adding a line (`POST /reports/expenses/lines`) or changing one
  (`PATCH /expense-lines/{id}`) takes the six details back - `original_currency`, `original_amount`,
  `exchange_rate`, `exchange_rate_date`, `exchange_rate_basis`, `exchange_rate_source` - and
  `fx.saved_exchange` checks them: all six or none, another currency than the company's, a rate
  above 0 for a day not after today, a known basis, Frankfurter, and original x rate giving the
  line's amount; anything else is 422 "The exchange-rate details don't match this amount. Read the
  receipt again or enter the amount yourself." Changing a line's amount without them clears them.
  The web sends them only while the amount is still the converted one. Xero is unchanged: the line's
  `amount` is the company-currency figure it always was.
- `FX_API_URL` moves the address (default `https://api.frankfurter.dev/v1`). Tests:
  `petty_cash/tests/test_expense_exchange.py`.

## Provider

**Gemini through the direct API, paid tier** (a decision of 3 September 2026); the Vertex AI route
(`asia-southeast1`, IAM instead of a key, in-region processing) remains implemented and is selected
automatically when `GOOGLE_CLOUD_PROJECT` is set. The paid tier settles that content is not used to
train and no human reads it; it does **not** settle residency - receipts cross a border they
otherwise do not - which belongs in the customer disclosure. The free tier is unusable for customer
receipts, and because the SDK cannot tell the tiers apart the tier is stated in configuration and
the engine shouts when Google's quota errors contradict it.

## Configuration

| Variable | Meaning |
|---|---|
| `EXPENSE_AI_ENABLED` | off unless on - the expenses page says `reader.enabled: false` (no button), the route answers `disabled` and the app shows no suggestions |
| `ai_policy` (database) | the models (primary, backup) and the limit rules - above |
| `EXPENSE_AI_MODEL` | only when no models are given (the check command without a policy row) |
| `EXPENSE_AI_THINKING_LEVEL` (`low`), `EXPENSE_AI_MAX_OUTPUT_TOKENS` (4096), `EXPENSE_AI_TIMEOUT_S` (25, per model tried) | the call |
| `EXPENSE_AI_CONF_HIGH` (0.85), `EXPENSE_AI_CONF_MEDIUM` (0.60) | the confidence bands |
| `EXPENSE_AI_MAX_FILE_MB` (10) | the receipt size cap |
| `GEMINI_API_KEY`, `EXPENSE_AI_ALLOW_DIRECT_API`, `EXPENSE_AI_DIRECT_TIER`, `EXPENSE_AI_USE_VERTEX`, `EXPENSE_AI_LOCATION`, `GOOGLE_CLOUD_PROJECT` | the route and the tier assertion - shared by every reader |

## Tests

`petty_cash/tests/test_expense_extract.py` - the route (who may ask, the file refusals, the silent
reasons), the allowance (the last read refused without asking the model, a failed read uses none,
shared with Payment Request, a token rule and its window, a stale pending read, the per-company
lock, no or a bad policy = off), the failover and its record, a model change taking effect on the
next read, the `ai_policy` command, the context sent (only switched-on accounts, a stable order), the reply checks (ids not
sent are dropped, bounds, bands, the detected supplier name), the one log line, and the check
command; the model is a stand-in. The engine's own cases (retries, timeouts, 429/5xx, cost, the
tier tripwire) are minty-shared-py's `tests/test_ai.py`.
