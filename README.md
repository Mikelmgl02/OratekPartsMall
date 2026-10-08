# MotionPartes

Brand: **MotionPartes by Oratek**. Domain: `motionpartes.com`. The local development site remains at http://localhost:8080/; public domain hosting and HTTPS are not configured yet.

Django REST Framework foundation for an invitation-only auto-parts quotation marketplace. Catalog and inventory expose no public prices. Prices belong to private, versioned deal quotations; each supplier's own price lists and per-client rules only prepare suggestions for its quotations (see **Precios privados y cotización asistida**).

## Run with Docker Compose

The Compose setup follows APIAG-CLOUD's structure, with `db`, `app`, `frontend`, and `nginx`: PostgreSQL 15, Django/Gunicorn, Next.js, and Nginx. Ports default to 8080 for the website/API and 5433 for PostgreSQL so it can run alongside APIAG-CLOUD.

```sh
cp .env.example .env
# Set your own DJANGO_SECRET_KEY and POSTGRES_PASSWORD in .env.
docker compose up -d --build --wait
docker compose exec app python manage.py createsuperuser
```

Open http://localhost:8080/ for the marketplace, http://localhost:8080/administracion for the MotionPartes admin panel, and http://localhost:8080/api/docs/ for API documentation. The `/health/` endpoint checks database connectivity. The database must pass its health check before the app starts; the app applies migrations, collects static files, and starts Gunicorn. Nginx waits for both the app and frontend to be healthy.

Compose automatically reads `.env`. Host-side Python does not. `POSTGRES_HOST=db` and `POSTGRES_PORT=5432` are always supplied inside the app container; host-side database tools use `localhost:5433`. Change `API_PORT` or `POSTGRES_PUBLISHED_PORT` to adjust the published ports. Both ports bind to the local machine only.

Source code is mounted into the app for development. After editing Python files, restart the app with `docker compose restart app`; rebuild with `docker compose up -d --build --wait` after dependency or Dockerfile changes. No APIAG database or services are shared.

```sh
docker compose logs -f app
docker compose exec app python manage.py test
docker compose exec app python manage.py check
docker compose down
```

`db_data` persists the database when containers stop or are recreated. `docker compose down` preserves it; `docker compose down -v` deletes it. Changing PostgreSQL credentials in `.env` does not update an already initialized database; alter its credentials deliberately before changing the configuration. This setup is for local development; public deployment still needs domain/HTTPS configuration and removal of the source bind mount.

## Frontend

All customer-facing website text must be in Spanish, including navigation, forms, accessibility labels, and error messages. Django uses Spanish for validation and administration; dates use the Spanish Panama locale and the America/Panama time zone. Keep API identifiers and supplier-provided catalog data unchanged.

Catalog entry fields use uppercase for internal SKUs, optional part names, descriptions, alterno codes and optional code brands. The frontend converts typed or pasted text while preserving the cursor, and the management API normalizes it before validation and storage. Admin forms and search fields disable browser autocomplete; catalog fields use neutral names to avoid personal-contact autofill heuristics. Authentication credentials, email addresses and supplier inventory IDs retain their functional format.

`frontend/` contains the Next.js App Router application using TypeScript, React, custom responsive CSS, bundled fonts, and Lucide icons. Its initial features are:

- Clearly labeled sample catalog for signed-out visitors, with illustrated parts, search, category shortcuts, grid/list views, and sample supplier selection.
- Invitation signup and sign-in connected to Django. Users without an assigned account see a setup-pending message.
- Account switching with explicitly assigned client/supplier capabilities. The live catalog uses Django's search and pagination; selecting an internal SKU shows its available supplier items with their individual codes and brands.
- Catalog filter sidebar with collapsible grupo, subgrupo, existencias and proveedor sections, full-catalog counts, removable selections and a mobile drawer. `catalog/` accepts repeated `category`, `subcategory` and `supplier` parameters (OR within a dimension, AND between dimensions); `category=` or `subcategory=` selects unclassified values. `availability` accepts `in_stock` (any positive band), `low`, `medium`, `high`, `sold_out` or `unknown`. `include_facets=1` adds filter option counts before pagination, scoped to the search and other filters. Groups remain browsable independently of the chosen subgroups; subgroups follow selected groups. Stock bands aggregate all active eligible suppliers for each canonical SKU; supplier filters match linked supplier inventory, including sold-out records. Prices and exact stock quantities remain private.
- Catalog cards include description, **Grupo / Subgrupo** (stored as `category` / `subcategory`), alternate codes, the number of suppliers with available stock, and the most recent stock update. `availability` in the catalog API contains only `status`, `supplier_count`, and `updated_at`; exact quantities remain private. Bands aggregate `max(0, reported_quantity - reserved_quantity)` across matched items from active supplier accounts: `sold_out` for zero, `low` for 1–5 units, `medium` for 6–20 units and `high` above that (defaults). `unknown` means no eligible supplier stock records exist. Configure the cutoffs with `CATALOG_LOW_STOCK_THRESHOLD` (default 5) and `CATALOG_HIGH_STOCK_THRESHOLD` (default 20); startup fails with `ImproperlyConfigured` unless the high threshold is greater than the low one. Clients receive the band name, never units or thresholds. Summaries use one stock query for the current page, without multiplying quantities for alternate codes or multiple supplier roles. Group and subgroup are also searchable; missing classifications display **SIN CLASIFICAR**.
- Supplier workspace with stock records, matching statuses, manual stock updates, resumable Excel balance imports, paginated credit/debit ledger history, private client requests in **Solicitudes** and private prices, client profiles and rules in **Precios**.
- Draft request basket grouped by supplier, retaining the selected supplier item for each SKU. Different supplier items under one SKU have separate basket lines. Drafts are saved in local browser storage, separately for each active account and for preview mode. Client accounts submit the basket as separate requests per supplier; successful sends move their items from **Borrador** to database-backed **Enviadas**. Orders and private quotations continue in the deal workflow; delivery and payments remain separate.

The supplier quotation editor uses **AG Grid Enterprise 33.3.2**, matching Essamobileapp's installed version. Supplier quantities and prices are editable; item identities and requested quantities are read-only. It supports sorting, column resizing, range copy/paste from Excel, Spanish menus and exact calculations in cents. Blank prices are allowed only for unavailable items (offered quantity zero). Edits autosave to the supplier's private server draft; sending commits the current cell, validates all rows, saves pending edits and publishes exactly that draft version.

Set `NEXT_PUBLIC_AG_GRID_LICENSE_KEY` in the root `.env` before building with Compose, or in `frontend/.env.local` for local frontend development. The license is registered before the grid is created. Compose provides the environment file through a BuildKit secret for the build step; only the AG Grid license is passed to the frontend build. AG Grid's browser-side license is included in the browser bundle as required by the library; other application credentials remain server-side. Environment files are ignored by Git. After changing only the license, run `docker compose build --no-cache frontend` before recreating the frontend, because build secrets do not invalidate Docker's cache.

**Cesta** opens a full-page cart with supplier item cards on the left and a sticky request summary on the right. Quantities and removal update the saved draft and summary immediately. On mobile, the summary stacks below the items. The `?vista=cesta` URL restores the cart after reload and supports browser back/forward navigation; the last selected account is remembered separately for each signed-in user. Product prices remain private. **Enviar solicitud** sends the selected supplier items and quantities; sample baskets cannot be submitted. The browser stores a submission ID before sending so retries after a lost response or reload recover the original request instead of duplicating it. After confirmation, submitted draft items are cleared and **Enviadas** opens with supplier references. New draft items receive a new submission ID, even when they repeat an earlier request.

Part details derive **Agregado** and **En cesta** quantities from the account's saved draft instead of temporary modal state. Closing/reopening, reload, quantity edits and removal retain the correct item state. **Agregado** opens the basket; **Agregar más** adds the selected quantity. The SKU summary and supplier item badges show draft units and successfully sent units split into **Enviadas · Por revisar** and **En revisión**, **Por confirmar**, **En ajuste** and **En acuerdos**, without repeating a sent total as an additional state. `GET /api/v1/accounts/<client_id>/catalog/<part_id>/request-state/` aggregates all of that client's saved request lines for the SKU, including historical snapshots of merged SKUs, without depending on history pagination. Supplier item badges match the item's supplier, ID, captured code and brand; changed codes are not mistaken for previously requested items. Errors loading sent quantities show an independent retry and preserve basket operations. This endpoint exposes the client's own requested and agreed quantities, without supplier stock or internal inventory IDs. Sending a request does not imply supplier acceptance or reservation.

The API token is stored in an HttpOnly, SameSite cookie by Next.js. Browser requests use a restricted Next.js API proxy; tokens are never returned to frontend JavaScript or saved in local storage. Mutating frontend routes check request origin. Set `COOKIE_SECURE=1` when deploying over HTTPS. The direct Django API remains available under `/api/v1/`, its documentation under `/api/docs/`, and the admin under `/admin/`.

For frontend development with the Docker API running:

```sh
cd frontend
npm ci
cp .env.example .env.local
npm run dev
```

Open http://localhost:3000/. The local frontend connects through http://127.0.0.1:8080/ to Django; in Docker it connects to `app:8000`. After frontend changes, rebuild the container with `docker compose up -d --build --wait frontend`. The frontend production container does not mount source files.

```sh
cd frontend
npm run typecheck
npm run build
npx playwright install chromium
npm run test:e2e
```

Browser tests cover preview search and basket persistence, mobile layout and invitation form, API-proxy access restrictions, supplier request search/detail/review, account switching, submission retries, and stale basket item recovery. Management tests check anonymous/ordinary-user denial and the superuser account, membership, user, and invitation workflow. Supply disposable `E2E_ADMIN_USERNAME` / `E2E_ADMIN_PASSWORD` credentials for the management workflow; this superuser may have no business memberships. The workflow creates a temporary account named `<admin username>-empresa` and an invitation for `<admin username>@invited.example.invalid`, and assigns/edits the ordinary user fixture, so clean up those fixtures after testing. The live supplier test additionally runs when `E2E_USERNAME` and `E2E_PASSWORD` are supplied. Its fixture needs a disposable account with both supplier and client roles, plus a catalog SKU equal to the uppercased username, a name equal to the username, and the equivalent code `FE-QA-001` with optional code brand `FE-QA`. It creates a stock record and tests its catalog mapping, ledger, and supplier offer, so use a dedicated test account. `E2E_BASE_URL` can override the default http://localhost:8080/.

The native catalog workflow creates a SKU derived from `<admin username>-catálogo`, checks multiple codes and duplicate rejection, edits and deactivates the SKU, reviews a supplier item named `<admin username>-stable`, and verifies that its ledger is unchanged. It creates/edits/retires an alterno code from **Alternos**, verifies that both editors show the same code library, and ingests a supplier item that matches through that alterno. A separate workflow creates `<admin username>-GRUPO` without a name or brand, ingests three equivalent supplier codes from different brands, and verifies their display and separate basket choices after reloading. For cleanup, remove the disposable supplier's stock updates and ledger entries before removing the test SKUs and their codes. These workflows also check the SKU editor, alterno editor and supplier choices at a 390px mobile viewport.

## Run locally

Requires Python 3.12–3.14. From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DJANGO_DEBUG=1
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/admin/ for account and catalog setup, and http://127.0.0.1:8000/api/docs/ for interactive API documentation. The documentation and schema are readable without signing in; protected API endpoints require a token or an authenticated session. `.env.example` documents configuration; environment files are not loaded automatically by Python. Export values in your shell or configure them in your deployment environment.

SQLite is for local development. Set the `POSTGRES_*` variables in `.env.example` for PostgreSQL, which is required for concurrent production inventory writes. Use a strong secret, DEBUG disabled, explicit allowed hosts, and HTTPS in deployment. Production hosting and background ERP scheduling are not configured yet.

## Accounts and invitation signup

An administrator creates an invitation with recipient email and expiration in the admin. Share its token privately with the recipient. Signup creates a user only. Then an administrator creates or selects an account, assigns its explicitly enabled roles, and adds employee memberships. Users can belong to multiple accounts, and accounts can carry several roles.

Seeded roles: `supplier_retail`, `supplier_wholesale`, `client_business`, `client_walkin`. Additional roles can be added in the admin. Membership owner/manager/staff values establish the structure; this initial inventory API allows all members of a supplier account to ingest its inventory. Private pricing uses them for its configuration and publishing permissions (staff by default). Employee management is admin-only for now.

## Supplier stock imports

Supplier account members can open **Para proveedores → Importar Excel**, download the template, upload an `.xlsx`, and select **Revisar archivo**. The preview compares current stock with the uploaded absolute balance. A change from 5 to 10 creates a movement of **5 Crédito**; a change from 5 to 0 creates **5 Débito**. Unchanged balances create no movement. Neither preview nor template download writes inventory.

The required headers are `ID_INVENTARIO_PROVEEDOR`, `CODIGO`, and `EXISTENCIAS`; optional headers are `MARCA` and `DESCRIPCION`. Header order is flexible, and English aliases are accepted. Use the supplier's permanent inventory ID, with one row per item. IDs retain their case and initial zeros, while product codes, brands and descriptions are uppercase. Stock must be a nonnegative whole number. A blank stock cell is an error; an explicit zero clears that item's reported stock. Items missing from the file retain their balances. When optional metadata columns are absent, existing metadata is preserved; a blank cell in a supplied metadata column clears that value.

Valid rows import in atomic batches of 500, without an application row-count limit. Files may be up to 5 MB, with a 30 MB decompressed-workbook limit. Progress can be paused and resumed after reload, and repeating a committed batch does not duplicate ledger movements. Rejected rows are retained in an account- and owner-bound job and can be downloaded as a correction workbook, corrected, then reuploaded. Those reports remain available after the seven-day commit window. Duplicate inventory IDs reject all occurrences; formulas, date-converted identifiers, invalid balances and items managed by apiag-cloud are excluded. Unknown product codes retain pending matching status and do not create catalog SKUs.

Imports preserve supplier item UUIDs, mappings, reservations and stock history. Every batch checks the stock and catalog mappings against its preview before committing; a changed snapshot requires a new preview while earlier completed batches remain saved. Existing apiag-cloud stock must continue to be updated through its ERP source. Reservations are retained even when reported stock becomes lower than reserved stock; available stock is floored at zero.

## Supplier orders and deals

**Borrador** (`?vista=cesta`) contains editable browser-local cart items. Sending removes the submitted items from the draft and opens database-backed **Enviadas** (`?vista=solicitudes`). One private order is created per supplier and client account. Later submissions append to the oldest `pending` order for that pair, accumulating quantities for the same supplier item, capped at 9,999. Once review starts, later sends create or append to another pending order. Existing orders and their item identities are preserved. Contribution records retain each cart addition and its original submission receipt; replaying the same UUID returns the exact saved receipt even if the order has grown or progressed.

The supplier opens an order using `POST /api/v1/accounts/<supplier_id>/requests/<order_id>/review/`. This idempotent transition records the reviewing employee, locks the order's items and moves it to `reviewed` (**En revisión**). GET remains a read-only operation. Supplier-only stock details are advisory and are never exposed to clients.

The following transitions are enforced by the API as well as the interface:

| State | Allowed decision | Next state |
| --- | --- | --- |
| `pending` | Supplier opens the order | `reviewed` |
| `reviewed` | Supplier confirms and sends a quotation | `quoted` |
| `quoted` | Client requests an adjustment with a reason | `adjustment` |
| `adjustment` | Supplier publishes a new revision or returns the unchanged quotation | `quoted` |
| `quoted` | Client accepts the exact current quotation | `handshaked` |

`POST /api/v1/accounts/<account_id>/deals/<order_id>/actions/` accepts `action=quote|accept|request_adjustment|return_quote`, UUID `operation_id`, and `expected_version`. Quote decisions include `quotation_id`. Publishing includes all original `order_line_id` values exactly once, offered whole `quantity` (0 for unavailable lines), nonnegative `unit_price` with two decimals, `currency=USD|PAB`, optional uppercase `terms` and the `draft_version` of the supplier's saved draft, whose values the payload must repeat exactly (409 otherwise; see **Precios privados y cotización asistida**). At least one offered unit is required. Prices/totals are computed with Decimal on the server. Publication constitutes supplier confirmation; client acceptance records the other confirmation and closes the agreement as **HANDSHAKED**. Published line prices and terms remain immutable, and adjustments create new revisions. Confirmed agreements appear in **Historial de compras** (`?vista=compras`), with agreed units, without implying delivery completion.

Account membership, participant identity and supplier/client capability are checked on every operation. Order row locks serialize review, additions and quotation decisions; stale versions and previous quotations return 409. Durable operation keys make lost responses safely retryable. An event history records decisions and the responsible account employee. Quotes remain private between the two accounts. Sending, quoting and handshaking do not change stock balances or create ledger entries; stock allocation, delivery and payment need their own workflow.

### Availability checks before mutual confirmation

The supplier's quotation draft (`GET/POST .../requests/<order_id>/draft/`) carries alerts computed on the server against live stock: **block** (`quantity_missing`, `price_missing`, `all_zero`, `draft_outdated`), **confirm** (`offered_gt_available` "5>3", `offered_gt_requested` "6>4", `identity_changed`, `zero_price`) and **info** (`reduced_to_stock`, `zero_offered`). In the editor, **Alertas** summarizes each line and **Revisa antes de enviar** lists them; **Confirmo** saves `lines[].acknowledge: [{code, context}]` (or `revoke: [code]`) in the draft. A confirmation counts only while its context is unchanged, so another quantity, price or stock level needs a new one. **Ajustar a disponibles** offers min(requested, available) on lines above stock. Suppliers can turn offering above stock or above the requested units into a block (`over_stock_policy` / `over_request_policy=block` in `pricing/settings/`), which no confirmation lifts.

Publishing a draft-bound quote returns 409 `{detail, exceptions}` while any alert blocks or awaits confirmation. Only with `QUOTES_REQUIRE_DRAFT=0` (legacy API clients) is a quote accepted without a draft; it then enforces only those block policies and records the remaining alerts as unconfirmed. Every publication stores a supplier-only trace: available units and item identity at quote time, the server-derived price and quantity source, and each alert with who confirmed it and when. `GET .../requests/<order_id>/quotations/<quotation_id>/trace/` returns it (supplier members only; `{"available": false}` for quotations published before this release); the order's **Ver origen de precios** shows it.

`return_quote` and `accept` re-check live stock for each offered line. Only stock that dropped since the quote counts: a line falls short when fewer than `min(offered, available_at_quote)` units remain, or when its item identity changed after the quote. A line confirmed above stock ("5>3") is short only below the 3 units the supplier relied on. Quotations published before this release are exempt. A short `return_quote` returns 409. A short `accept` is blocked by default (`accept_shortfall_policy=block`): it returns 409 with a message that contains no quantities, the deal stays `quoted`, no command is stored, and the check is recorded privately for the supplier. The client sees that message and **Solicitar ajuste**. With `allow`, the accept proceeds and is recorded as a shortfall. The supplier's order list shows **Confirmación bloqueada por existencias** or **Confirmado con faltante** (`availability_alert`); client payloads gain no keys. These checks read stock without locking and never write it: no reservation, deduction or ledger entry is created.

Each deal also has a persistent private conversation: `GET/POST .../deals/<order_id>/messages/`. Posting includes UUID `message_id` and uppercase `body` (up to 4,000 characters). Retry keys prevent duplicate messages. GET returns up to 100 messages with `cursor`, `has_more` and `has_earlier`; use `after` for new messages or `before` for earlier history. The interface refreshes chat every five seconds and deal/list state every fifteen seconds while the tab is visible. Chat is also available after handshake for delivery coordination; chat messages never change agreement terms. Older messages can be loaded without losing newer ones.

Order lists retain the existing `/requests/` and `/sent-requests/` routes, with account-scoped search, pagination and all five status filters. `GET .../catalog/<part_id>/request-state/` reports units in mutually exclusive lifecycle states, including historical merged-SKU snapshots. Open orders (`pending`, `reviewed`, `quoted`, `adjustment`) count requested units; `handshaked_quantity` (**En acuerdos**) counts the units of the quotation the client accepted, which can differ from the requested units (0 for lines quoted as unavailable). Requested units the supplier did not agree to are not reported as a state; they remain visible on the order (requested line `quantity` vs. the accepted quotation, `unit_count` vs. `quoted_unit_count`). `sent_quantity` remains the total of requested units. Stock figures remain supplier-only, including reported/reserved/available quantities and shortfalls.

## Precios privados y cotización asistida

Each supplier prices its quotations from its own private data. Everything below is supplier-only: catalog, offers, client payloads, deal events, Django admin and Oratek management never carry supplier prices, profiles, rules, drafts or assistant output, and clients see only the prices of quotations sent to them. `mall/test_pricing_privacy.py` seeds private markers and pins the client-facing key sets. Suppliers work in **Precios** (**Listas de precios**, **Clientes**, **Reglas**, **Simulador**, **Historial**, **Configuración**) and on each order's **Cotización** tab.

**Data model** (additive tables; none is registered in Django admin):

- `SupplierPricingSettings` (one per supplier: default currency, USD/PAB parity, configuration and publish permissions, alert policies, accept shortfall policy, quantity prefill, assistant opt-in) and `PricingAuditEvent`, the private **Historial**.
- `PriceList` / `PriceListEntry` (several named lists, one default; a list's currency is fixed once it has prices), `SupplierItemPricing` (the supplier's LINEA discount group and floor price) and the append-only `PriceChange`. Stored outside `SupplierItem`, so price writes never change stock rows, catalog `availability.updated_at` or stock import fingerprints.
- `ClientPricingProfile` (one per supplier–client pair that has ordered: list, fallback to the default list, general discount, preferred currency, default terms, ERP customer code, internal notes) and `PricingRule` (scope all or client; target all, LINEA, brand or item; discount or net price; minimum quantity; validity window).
- `DealQuotationDraft` / `DealQuotationDraftLine` (the server draft) and `DealQuotationAudit` / `DealQuotationLineAudit` (the immutable trace of each publication).
- `PriceImportJob` / `PriceImportBatch` (separate from the stock import tables, so a price import never pauses matching).
- `QuoteAssistantRun` and `AIUsageRecord` (AI runs and daily spend per feature).

**Engine and precedence (decision D2).** `mall/pricing_engine.py` computes each suggested price from that data alone, with Decimal and a constant number of queries. The client's agreement comes first (its client-scoped rules and the profile's general discount), then the most specific target (item, then LINEA, then brand, then all), then the highest minimum quantity. Exactly one rule applies per line, never stacked; a remaining tie takes the higher price. The result is rounded once, ROUND_HALF_UP to 0.01. A list in the other currency is used only at 1:1 USD/PAB parity, when enabled. Every suggestion carries a step-by-step explanation (**¿De dónde sale este precio?**) and a fingerprint; the **Simulador** shows the outcome before saving a rule.

**Drafts and alerts.** Every order in `reviewed` or `adjustment` has one private draft (`GET/POST .../requests/<order_id>/draft/`). Reading it writes nothing: it stays virtual until the first save. Saves are partial, versioned (`draft_version`), retry-safe (`save_id`) and autosaved, and never change the order's version or write client-visible activity. Values are prefilled from the saved draft, then the previous revision, then the engine, then blank; a price's source is `engine`, `previous`, `manual` or `none`, never AI. Server-computed alerts block, need a confirmation tied to the value on screen, or inform (see **Availability checks before mutual confirmation**). **Publishing requires the reviewed draft** (`QUOTES_REQUIRE_DRAFT=1`, the default): the `quote` action must name the saved `draft_version` and repeat its values exactly, so a draftless quote returns 409. If the editor cannot load the draft it shows the previous revision read-only with **Reintentar** instead of a send button. Publication consumes the draft and stores the trace (stock at quote time, suggested versus final price, server-derived sources, confirmations, applied assistant run), shown under **Ver origen de precios**. Members below `publish_min_permission` use **Solicitar aprobación**; editing quantities, prices, currency or terms afterwards withdraws the request, and both events appear in **Historial**.

**Availability (decision D1).** Accepting is blocked when stock the supplier relied on dropped after the quote (below `min(offered, available at quote)`, or the item's identity changed). A line confirmed above stock counts only the units that were available at the quote, quotations published before the trace are exempt, and a supplier can choose `accept_shortfall_policy=allow`. The client receives a message without quantities and **Solicitar ajuste**.

**Excel price import** (**Listas de precios → Importar Excel**, template and export in the same format): an `.xlsx` up to 5 MB, read from the `PRECIOS` sheet (or `LISTA_PRECIOS`, or the first sheet).

| Column | Meaning |
| --- | --- |
| `ID_INVENTARIO_PROVEEDOR` | Required; the stock import's aliases are accepted. Rows are keyed by it. |
| `CODIGO` | Optional check only; a different code warns but never blocks the row. |
| `PRECIO` | The default list (created as `GENERAL` when the supplier has none). |
| `PRECIO_<CÓDIGO>` | A named list; new lists need explicit confirmation before the first batch. |
| `LINEA` (`FAMILIA`, `GRUPO_DESCUENTO`) | The supplier's discount group used by LINEA rules. |
| `PRECIO_MINIMO` (`PRECIO_PISO`) | Floor price: offering below it needs a confirmation; the engine never clamps. |

`MARCA` and `DESCRIPCION` are ignored but kept in the correction workbook; cost columns (`COSTO`, `PRECIO_COSTO`, `PRECIO_COMPRA`) and unknown columns are ignored and listed. A blank cell keeps the value, `BORRAR` removes it and `0` is skipped with a warning. Amounts are parsed as Decimal: ambiguous separators and more than two decimals are rejected, never rounded. The preview writes nothing; batches of 1,000 rows apply with a confirmation of changes above 50 %, the job expires after 7 days, and rejected rows download as a correction workbook.

**Permissions (decisions D4 and D5).** Any member of the supplier account reads prices, lists, profiles, rules, drafts and traces, saves drafts and runs the assistant. Writing prices, lists, imports, profiles, rules and the other settings needs `config_min_permission`, and `quote` / `return_quote` need `publish_min_permission` (both staff by default; a refused member gets 403 before any version check). Only the owner changes permission settings and the AI opt-in. Run `python -m mall.pricing_preflight --check` before a supplier tightens permissions: it lists supplier accounts with no active owner or manager. Oratek superusers have no access to supplier prices, profiles or rules (supplier routes return 404); **Estadísticas** shows them aggregates without prices only.

**AI quotation assistant.** **Interpretar solicitud del cliente** sends the order lines and the client's order notes, latest adjustment reason and chat since the latest revision (capped, with account and member names, emails, written amounts and every figure the supplier typed masked) to Gemini; it never sends prices, lists, rules, profiles, stock or other orders. It returns proposals (quantity change, remove line, internal line note, terms item; price requests, items outside the order and questions can only be dismissed), each with a verbatim quote of the client that the server verifies. The response schema has no price field and money text is dropped. Nothing changes the draft until a supplier applies a proposal, which is a draft save; an engine price then follows the new quantity deterministically. An identical interpretation within 30 days is a free cache hit that keeps the decisions already taken, the same note or terms text is never appended twice, and once the owner turns the assistant off its stored proposals can no longer be applied (403). Runs are capped per revision, per account and day, by the assistant's monthly budget and by the platform's, with a pre-call cost estimate.

It ships dark. To enable it:

1. Set `GEMINI_API_KEY` (and `GEMINI_MODEL`).
2. Set `GEMINI_INPUT_USD_PER_MTOK`, `GEMINI_OUTPUT_USD_PER_MTOK` and `GEMINI_GROUNDING_USD_PER_CALL` from Google's current price sheet for that model. The defaults are placeholders, and every spend figure and cap depends on them.
3. Set `QUOTE_ASSISTANT_MONTHLY_USD` above 0 (40 of the 200 platform budget is the recommended cap) and recreate the `app` and `matching` containers.
4. Publish the privacy-policy and terms disclosure: Google Gemini processes order and chat text when a supplier enables the assistant.
5. Each supplier's owner opts in under **Precios → Configuración → Asistente de IA** (after reading the disclosure there); its clients then see a notice in the deal chat.
6. Schedule the retention purge below.

**AI budget.** Every Gemini feature records its daily spend in `AIUsageRecord`: the assistant per supplier, and catalog classification, the inventory assistant, SKU grouping, part types, OEM lookup and supplier matching as platform usage (reported tokens at the configured rates, the grounding fee of an OEM search, or the estimate of a failed call that may have been billed). `AI_MONTHLY_BUDGET_USD` therefore covers all AI spend: the assistant refuses runs once the platform total would exceed it, catalog features keep working and are counted, and **Estadísticas** shows the month's spend by feature against the budget.

| Variable | Default | Purpose |
| --- | --- | --- |
| `QUOTES_REQUIRE_DRAFT` | `1` | Quotations are published only from the reviewed draft; `0` re-opens the draftless legacy API path. |
| `QUOTE_ASSISTANT_MONTHLY_USD` | `0` | Assistant monthly cap; 0 keeps it off. |
| `AI_MONTHLY_BUDGET_USD` | `200` | Platform-wide monthly AI budget. |
| `QUOTE_ASSISTANT_RUNS_PER_REVISION` | `5` | Billable runs per draft revision. |
| `QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT` | `30` | Billable runs per supplier and day. |
| `GEMINI_INPUT_USD_PER_MTOK` / `GEMINI_OUTPUT_USD_PER_MTOK` | `1.00` / `5.00` | Placeholder token prices (thinking tokens at the output rate). |
| `GEMINI_GROUNDING_USD_PER_CALL` | `0.035` | Placeholder fee per Google Search grounded request. |

**Retention purge.** `python -m mall.purge_quote_assistant [--dry-run]` blanks the summary, notes, terms texts, questions and quoted evidence of assistant runs older than 30 days (a cached copy goes with its source); runs, decisions, metrics and the trace's links are kept. It also deletes the staged batches of expired price imports and each job 30 days after it expired. It is idempotent and prints counts only. No extra container is needed; schedule it daily from the host, for example with cron:

```sh
15 3 * * * cd /path/to/OratekPartsMall && docker compose exec -T app python -m mall.purge_quote_assistant >> /var/log/motionpartes-purge.log 2>&1
```

Supplier routes, all under `/api/v1/accounts/<supplier_id>/` (any member reads; writes follow the permissions above):

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `pricing/settings/` | Settings (currency, parity, permissions, policies, assistant opt-in) |
| GET | `pricing/history/` | Private audit log |
| GET / POST, POST | `price-lists/`, `price-lists/{id}/` | Price lists: list and create, then edit one |
| GET / POST | `prices/` | Price grid and manual edits; `prices/{item_id}/history/` for one item |
| GET | `prices/export/` | Excel export in the import format |
| GET / POST | `prices/import/template/`, `prices/import/`, `prices/import/jobs/{job_id}/` | Excel import template, preview and batches; `.../errors/` correction workbook |
| GET, GET / POST | `clients/`, `clients/{client_id}/profile/` | Clients that ordered and their private profile |
| GET / POST, POST | `pricing-rules/`, `pricing-rules/{id}/` and `{id}/archive/` | Commercial rules: list and create, then edit or archive one |
| POST | `pricing/simulate/` | Price lines for a client without saving |
| GET / POST | `requests/{order_id}/draft/` | Quotation draft; `discard/` and `reprice/` |
| GET / POST | `requests/{order_id}/draft/assistant/` | Assistant state and runs; `{run_id}/decisions/` applies or dismisses |
| GET | `requests/{order_id}/quotations/{quotation_id}/trace/` | Publication trace |

## Administration

Superusers can open `/administracion` from the marketplace's **Administración** link. A persistent sidebar provides **Inventario**, **Alternos**, **Cuentas**, **Usuarios**, and **Invitaciones**. It collapses behind **Menú administrativo** on mobile. Each section has a shareable URL, for example `/administracion?seccion=alternos`, and reloads retain the selected section. All management workflows use the Spanish MotionPartes panel and the current sign-in session.

In **Inventario → Inventario interno**, use **Crear SKU** to enter the internal SKU, optional name and description, and **Alternos (códigos equivalentes)** in one form. The internal SKU has no brand. An alterno with a blank brand matches supplier items from any brand; an optional code brand narrows the match when necessary. Conflicting codes assigned to other SKUs and duplicate SKUs are rejected, and the entire save is atomic. **Editar** manages details, multiple codes and active status while preserving the part ID. Removing a code does not reassign supplier records. SKUs can be deactivated rather than deleted. In **Existencias por proveedor**, use **Revisar** to select a catalog SKU and approve its matching status; supplier IDs, quantities and stock ledger entries are preserved. Creating a catalog code does not automatically approve pending stock.

The **Alternos** section manages that same library one code at a time: select the **SKU interno**, enter the **Código alterno**, and optionally its brand. Codes added here immediately appear in the SKU editor and in customer catalog searches; codes added in the SKU editor appear here. Customer and admin catalog searches also find a SKU through the OEM library: an OEM number it reaches or an aftermarket code filed under one, ignoring dashes and spaces (`16100 39317`, `gwt41a`), from four letters or digits. An alterno is a code suppliers use for the same physical part, not a second SKU or a relationship between different SKUs. The actual supplier, code and brand choices come from matched supplier stock records.

### Import internal inventory from Excel

Superusers can use **Inventario → Inventario interno → Importar Excel**. Download the template, replace its example rows, choose the completed `.xlsx`, and select **Revisar archivo**. The preview shows SKU names/descriptions, categories, active status, new alternos, and create/update counts. **Importar filas válidas** proceeds when some rows have errors: only accepted rows are staged in the catalog batches, while every rejected row is saved in **Errores de importación** with its original cells, description, suggestions and validation messages. Files with no row errors use **Confirmar importación**. Both actions save up to 500 grouped SKUs per atomic batch, with progress and **Pausar importación / Reanudar importación** controls.

The first row contains these headers (order is flexible):

| Column | Behavior |
| --- | --- |
| `SKU` | Required, stored as text. Repeated rows belong to the same internal SKU. |
| `NOMBRE` | Optional. Nonblank values update the SKU name. |
| `DESCRIPCION` | Optional. Nonblank values update its description. |
| `CODIGO_ALTERNO` | Optional verified equivalent reference for that SKU, stored as text. Use one row per reference. |
| `MARCA_ALTERNO` | Optional code brand; blank means all brands. Requires a code on that row. |
| `ACTIVO` | Optional `SI` or `NO`. Blank preserves an existing SKU's status; new SKUs default to active. |
| `CATEGORIA` | Optional uppercase product category. Blank preserves the current value. |
| `SUBCATEGORIA` | Optional uppercase product subcategory. Blank preserves the current value. |

Blank metadata cells preserve existing values. Existing SKUs keep their IDs, existing alternos are retained, and identical repeated codes are added only once. Repeated SKU rows must agree on supplied metadata and status. Text is normalized to uppercase. Numeric SKU/alterno cells are converted to text and listed as preview notices. Plain `General`, text, zero-padded integer, and decimal zero/optional-digit formats preserve their values; rounding, currency, percentage, scientific, and other display formats require the original code as text. Numeric values with more than 15 meaningful digits are rejected because Excel may already have lost precision. Existing text identifiers keep their initial zeros. SKU/code conflicts, formulas and invalid status values are rejected. The importer accepts `.xlsx` files up to 5 MB with no application row-count limit, reading the `Inventario` sheet or otherwise the first worksheet. A 30 MB decompressed-workbook limit bounds parsing memory.

The reader reconstructs date-converted identifiers when a complete code in the description or multiple distinct intact numeric code patterns provide evidence. It does not treat incidental vehicle year ranges as part-code evidence. Ambiguous dates show editable uppercase suggestions and alternative formats in **Códigos que Excel convirtió en fechas**; administrators can apply all reviewed corrections together or explicitly exclude individual affected rows, without editing the workbook. Automatically recovered rows are collapsed for review, and copied date values in `NOMBRE` become the recovered SKU. The preview, resumed job, AI review and completion message retain the original Excel row, recovery decisions and exclusion counts. Multipart previews accept `corrections` as a JSON array of `{row, column, value}` and `skip_rows` as a JSON array of row numbers. These decisions are permitted only for date-converted SKU/alterno cells and still pass normal catalog conflict checks. Recovery uses workbook evidence locally and requires no AI provider key. Format identifier columns as text before exporting when possible; a date value alone cannot distinguish every original code or its padding.

This imports the internal catalog only. Supplier inventory IDs, stock quantities, mappings, pending approvals and stock ledgers are preserved. Validated rows are staged in owner-only import jobs for seven days; the workbook itself is not stored. Upload once, then create successive batches using the job endpoint. The browser remembers the pending job and recovers server progress after reload or a lost response. Repeating an already committed batch index returns its saved checkpoint without committing the following batch. Each batch validates the relevant catalog snapshot again. If a later batch fails, that batch rolls back while previous batches remain saved; a changed catalog requires a new preview. Closing a paused dialog refreshes the catalog without reporting full completion.

Rejected rows remain in the current superuser's persistent **Errores de importación** queue beyond that draft window. Correct them after the accepted-row import finishes, then use **Guardar y reintentar** to validate and save a single row against the current catalog. Repairs are atomic and idempotent; conflicting codes remain pending with the latest edits. A file containing only invalid rows creates a review-only job, allowing immediate correction without catalog batch commits. Explicitly excluded date rows are also retained for later correction. When repeated rows for a readable SKU disagree or one is invalid, all rows of that SKU are held out together to avoid importing inconsistent metadata or partial alias assignments. Global workbook errors such as invalid headers still reject the file. Re-previewing the same uploaded file with its `job_id` replaces that unstarted draft's pending rows instead of creating duplicate queue entries.

Before importing, **Clasificar con IA** proposes Spanish categories/subcategories and equivalent-code groupings in 50-SKU batches. Proposals include reasons, confidence and source part-number evidence; administrators explicitly review, edit and select proposals before applying them to the staged preview. A complete base SKU followed by supplier letters can support a review proposal when the full nonblank descriptions match. Overlapping trailing application-year ranges may differ, with an explicit review warning; position, size, specification and numeric suffix variants remain separate. When at least two compatible supplier variants share a base that is absent, the system proposes that base as a new parent instead of requiring an existing parent. Only reviewed decisions create it, and code ownership conflicts prevent creation. Uncertain equivalences remain separate. Grouping adds the original source SKU and its codes as alternos of the chosen SKU. Applying proposals does not write the catalog until import is confirmed. Classification progress and reviewed decisions survive reloads; malformed provider output never becomes catalog data. The import action explicitly says **Importar sin IA** when classification has not run, or **Importar con revisión parcial** while proposals remain unreviewed.

**Inventario → Inventario interno → Asistente IA** classifies existing catalog items without uploading Excel. Select items missing a group/subgroup or the entire active canonical catalog, optionally filter by SKU/description/alterno and provide classification instructions. Analyses save their selected IDs and run sequential 50-SKU batches with no overall item-count limit. Pause after the current batch and reopen **Análisis guardados** to resume. Candidates are retrieved across the entire catalog even when the source filter is narrow; only bounded source/candidate context reaches Gemini. Each proposal shows source description, existing classification, suggested group/subgroup, candidate destination, reason and confidence. Proposals remain pending until an administrator edits and explicitly confirms individual rows, applies them, or discards them. Edits clear the review checkbox. Blank classification fields preserve existing metadata. Reviewed grouping uses the same atomic merge service and preserves supplier IDs, stock counters, ledgers and historical request lines. Changed descriptions, identifiers, removed aliases or conflicting category edits require a fresh analysis. Creating jobs, classifying batches and applying the same reviewed decisions are retry-safe. Jobs and their audit of reviewed decisions are owner-only and superuser-only; merely opening the assistant makes no provider request or catalog write.

For items already imported, **Inventario → Agrupar SKU** lists conservative supplier-suffix families. Review their descriptions and codes, optionally ask AI to examine just that family, select confirmed equivalent sources, and save one canonical SKU. Existing source rows become inactive merged records pointing to the canonical row; their UUIDs remain traceable, their codes become canonical alternos, and supplier inventory IDs, quantities and stock entries are preserved. Existing canonical UUIDs remain unchanged. A proposed missing parent is labeled **CREAR SKU PADRE** and is created only on confirmed review (`create_target: true`); all selected child codes become its alternos. Merges are atomic, auditable and idempotent; conflicting third-party code ownership blocks a merge. Reimporting a retired source SKU does not reactivate it: use its canonical SKU with the source code as an alterno. Similar names or prefixes alone never trigger automatic catalog merges.

Configure `GEMINI_API_KEY` (or the existing `GEM_API_KEY` environment variable) and optionally `GEMINI_MODEL`, then recreate the app service. The default model is `gemini-3.5-flash-lite`. The key remains server-side; starting classification sends codes and descriptions to Gemini and consumes the provider's usage. Without a key the panel explains that AI is unavailable, while ordinary batch import still works. AI request tests use mocked provider replies; a configured live provider is needed to validate its deployment credentials and quota.

Provider requests use header-based key authentication and `responseMimeType`/`responseJsonSchema` for structured JSON, matching the output settings used by apiag-cloud's Gemini SDK. Editing `.env` requires recreating the API container; a page refresh alone does not load a new key. The apiag-cloud settings `GEMINI_DAILY_TOKEN_LIMIT` and `GEMINI_DAILY_REQUEST_LIMIT` are not enforced by this classifier.

The provider response schema stays small and independent of batch size; fixed-length 50-item arrays cause Gemini to reject the request. The server still checks the exact source count, unique source IDs, allowed destinations, field types and grouping evidence. In the existing-inventory assistant, an unverified grouping is converted to a separate-SKU classification proposal with a review explanation and reduced confidence, so it cannot block unrelated classifications. Provider failures leave the current batch pending, and server diagnostics record only model, HTTP status and source count, never keys or inventory cells.

The panel also supports creating and editing accounts, assigning multiple client/supplier types, adding employee memberships, changing membership permissions, removing account access, editing user names and email addresses, activating/deactivating users, and creating or revoking invitations. New users still register by invitation; administrators then assign their accounts.

Access is checked against Django's current `is_superuser` flag on every management request and when rendering the page. Staff status, account ownership, and membership permissions do not grant administrative access. The existing Django admin at `/admin/` is also restricted to active superusers. Management endpoints use `/api/v1/management/`; the frontend forwards only permitted routes and checks the origin of every write. `/api/v1/auth/me/` supplies the signed-in user's identity and superuser status without exposing tokens.

Deactivation preserves account and inventory history. Deactivating a user revokes existing API tokens; superusers cannot be deactivated through this panel. Passwords and administrative privileges are managed separately in the superuser-only Django admin. Invitations show a code to share manually; no email is sent automatically.

## Internal analytics

Superusers can open **Administración → Estadísticas** to see 7-, 30- or 90-day activity in Panama time: visits, returning users, completed searches, searches without results, part-detail views, basket additions, requested units and supplier review times. Rankings highlight catalog gaps, popular SKUs, frequent visitors and suppliers with pending requests over 24 hours. **Cotizaciones y uso de IA** adds platform-wide quotation KPIs (time from sending to the first version, versions per quoted order, acceptance rate, accepts blocked by stock) and this month's AI spend by feature against `AI_MONTHLY_BUDGET_USD`; it shows no prices, totals, suppliers or clients and reads a constant number of queries. Fulfillment metrics can be added when that transition exists.

`POST /api/v1/analytics/events/` accepts authenticated visit activity, completed searches, part views and basket additions for an active member account. The server validates event references, calculates search result counts itself and deduplicates event UUIDs. A visit ends after 30 minutes of inactivity; its browser key is shared across tabs and stored in local storage. Hidden tabs do not produce activity heartbeats. Superuser and demo browsing are excluded, and telemetry failure does not block shopping. Basket-added quantities are cumulative additions during the period, not current basket balances. SKU merges retain historical demand metrics.

`GET /api/v1/management/analytics/?days=30` is restricted to superusers in both the frontend proxy and Django. Usage records stay in the application's PostgreSQL database and are linked to the authenticated user; no IP addresses, device fingerprints or external trackers are collected by this feature. The **Cómo funciona** dialog explains the collection to signed-in users. Searches and visits begin recording after deployment, while request counts and review durations use existing transaction history. These records currently have no scheduled retention cleanup.

## Endpoints

All routes use `/api/v1/` unless noted.

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `auth/signup/` | Accept an unexpired, single-use invitation |
| POST | `auth/login/` | Username/password to API token |
| POST | `auth/logout/` | Revoke token and end session |
| GET | `auth/me/` | Current identity and superuser status |
| POST | `analytics/events/` | Authenticated internal usage event, validated and retry-safe |
| GET | `management/analytics/` | Superuser demand and supplier review statistics; `days` accepts 7, 30 or 90 |
| GET / POST | `management/catalog/` | Superuser SKU directory and atomic creation with equivalent codes |
| GET / POST | `management/catalog/assistant/` | Owner-only saved analyses, eligible counts (`scope`, `search`) and retry-safe creation using a client UUID |
| GET / POST | `management/catalog/assistant/{job_id}/` | Paged proposals (`offset`), resumable `classify` batches, explicit `apply` decisions or `dismiss` IDs for existing inventory |
| POST | `management/catalog/import/` | Multipart `.xlsx` validation and staging; legacy signed confirmation commits only the first 500-SKU batch |
| GET / POST | `management/catalog/import/jobs/{job_id}/` | Owner-only progress and atomic next batch using `batch_index`; retries are idempotent |
| GET | `management/catalog/import/issues/` | Owner-only paginated pending or resolved import rows; filters `status`, `job`, `search` |
| GET / POST | `management/catalog/import/issues/{issue_id}/` | Inspect a source row or atomically validate and save its corrected `values` |
| GET / POST | `management/catalog/import/jobs/{job_id}/classification/` | Owner-only paged AI proposals, resumable 50-SKU classification, and explicitly reviewed decisions |
| GET | `management/catalog/grouping/` | Superuser-only paged candidate families; optional search |
| POST | `management/catalog/grouping/classify/` | Review one selected family with AI; no catalog writes |
| POST | `management/catalog/grouping/merge/` | Merge explicitly reviewed sources into a canonical SKU, preserving supplier stock identities |
| GET / PATCH | `management/catalog/{part_id}/` | Edit SKU details, equivalent codes and active status |
| GET | `management/catalog/{part_id}/oem-equivalents/` | Read-only: the SKU's OEM numbers (OEM alternos, the number its code names, catalog links) and the aftermarket codes filed under them |
| GET | `management/inventory/` | Superuser stock directory across suppliers |
| GET / PATCH | `management/inventory/{item_id}/` | Review catalog mapping and matching status only |
| GET / POST | `management/alternates/` | Superuser alterno code library and creation under an internal SKU |
| GET / PATCH / DELETE | `management/alternates/{id}/` | Inspect, edit or retire an alterno code |
| GET / POST | `management/accounts/` | Superuser account management |
| GET / PATCH | `management/accounts/{id}/` | Account details, roles, and activation |
| GET | `management/users/` | Superuser user directory |
| GET / PATCH | `management/users/{id}/` | User profile and activation |
| GET | `management/roles/` | Available account types |
| POST | `management/memberships/` | Assign user to account |
| PATCH / DELETE | `management/memberships/{id}/` | Change permission or remove account access |
| GET / POST | `management/invitations/` | List or create invitations |
| PATCH | `management/invitations/{id}/revoke/` | Revoke unused invitation |
| GET | `accounts/` | Current user's active accounts and roles |
| GET | `catalog/?search=ABC` | Internal SKUs and their alterno codes |
| GET | `catalog/{part_id}/suppliers/` | Available supplier groups with matching item IDs, codes, brands and descriptions |
| GET | `wishlist/?search=ABC&page=1` | Personal saved SKUs, newest first, with current stock bands and catalog availability |
| GET | `wishlist/state/` | Current user's saved part IDs and count for persistent hearts |
| PUT / DELETE | `wishlist/{part_id}/` | Idempotently save or remove a canonical SKU; never reserves stock or creates a request |
| GET | `accounts/{account_id}/inventory/` | Supplier's own stock and matching status |
| POST | `accounts/{account_id}/inventory/ingest/` | Apply one inventory balance update |
| GET | `accounts/{account_id}/inventory/import/template/` | Supplier-only Excel stock template |
| POST | `accounts/{account_id}/inventory/import/` | Multipart Excel validation and staging; no stock writes |
| GET / POST | `accounts/{account_id}/inventory/import/jobs/{job_id}/` | Owner- and supplier-bound progress or atomic `batch_index` commit |
| GET | `accounts/{account_id}/inventory/import/jobs/{job_id}/errors/` | Download retained rejected rows for correction and reupload |
| GET | `accounts/{account_id}/inventory/{item_id}/ledger/` | Supplier's stock history |
| GET | `/api/schema/` | OpenAPI schema |
| GET | `/api/docs/` | Interactive API docs |

The Spanish **Favoritos** view is personal to the signed-in user and persists in PostgreSQL across devices and account switches. Hearts on catalog cards and part details share the same state. Catalog merges transfer and deduplicate favorites while preserving their original save date. Inactive saved parts stay visible as **FUERA DEL CATÁLOGO** and can be removed; choose supplier options from an active favorite to add it to the request basket.

Authenticate using `Authorization: Token <token>` or a Django session with CSRF protection. The supplier account in the path is checked against the authenticated user's membership and supplier role.

Alterno creation example (`POST management/alternates/`, superusers only):

```json
{
  "part": "<internal SKU UUID>",
  "code": "58-1R0",
  "brand": ""
}
```

This creates a `PartCode` under the selected SKU. The response contains `id`, `part`, `part_sku`, `part_name`, `code` and `brand`. The customer catalog keeps these codes in its `codes` list; it no longer exposes the previous relationships between different SKUs.

Inventory update example:

```json
{
  "update_id": "erp-event-2026-001",
  "supplier_invent_id": "12345",
  "codigo": "ABC-123",
  "brand": "ACME",
  "description": "Oil filter",
  "source": "apiag",
  "quantity": 12
}
```

`quantity` is an absolute nonnegative whole-unit stock balance, not a delta. `update_id` must be unique within the supplier account. Retrying an identical normalized payload does not create another ledger adjustment; reusing its ID with different content fails. The response represents the current item state. Events must arrive in order; versioned ERP events remain future work. Missing items in an upload are not implicitly zeroed.

Ledger responses expose nonnegative `quantity` and `direction` (`credit` or `debit`), replacing the signed `stock_delta` field. `balance_type` identifies stock or reservation movements. Historical reservation changes also retain their positive `reserved_delta` and `reserved_direction` (blank for no reservation change). Migration `0010_positive_stock_ledger` converts existing history without changing record IDs, references, timestamps or resulting balances.

## Catalog identity and stock rules

- MotionPartes owns the stable catalog `Part.id` (our invent ID) and the unique, visible `Part.sku`. The internal SKU is brand-neutral; its name and description are optional.
- Identical parts with different codes or brands share one SKU. Those supplier codes are the SKU's **alternos**, stored as `PartCode` entries. For example, SKU `58411-1R000` can contain alternos `58411-1R000-G`, `58-1R0` and `D-HYU-1R`. Customers can search any of those codes, select the same SKU, and choose its actual supplier items by code and brand.
- The supplier owns `supplier_invent_id`, which must be stable and never reused for a different product. No remesa is tracked.
- Each supplier item has a permanent internal UUID. Ledger entries reference it, not its changeable codigo.
- SKUs and equivalent codes are normalized by trimming and uppercasing. Punctuation is retained to avoid unsafe matches. Equivalent codes may be brand-neutral or have an optional brand scope; there is no internal SKU brand.
- A supplier code matches the internal SKU itself or one of its equivalent codes. Brand-neutral codes match any supplier brand; scoped codes require the same brand. A unique active match is assigned automatically. The background matching pipeline also resolves normalized codes and full base-code families with identical detailed applications. A consistent family of at least two distinct supplier codes can create its missing parent automatically. Unknown single codes and ambiguous matches stay in the review queue.
- A code/brand change that still resolves uniquely to the current SKU stays matched, preserving the supplier item ID and ledger. A change that no longer resolves to that SKU requires review. The old mapping is retained internally but the item is excluded from client offers until approved.
- Both the SKU editor and **Alternos** manage the same `PartCode` rows. Each alterno identifies the same physical part under its internal SKU. Supplier stock IDs and ledger histories are independent of these changeable codes.
- Inventory source ownership is fixed on first ingestion (`upload` or `apiag`). Switching sources requires a future explicit reconciliation workflow.
- Every stock balance change records a positive movement quantity with a credit/debit direction, resulting balances, actor, timestamp, and update reference in an atomic transaction. Ledger records are read-only in the API and admin.
- Availability is reported stock minus reservations, floored at zero. Client offers reveal available supplier item IDs, codes, brands and descriptions, without quantities or prices; suppliers can inspect their own quantities. Several matching lines appear under one supplier group, and the basket retains each selected supplier item separately.

Migration `0005_brand_neutral_skus` derives initial SKUs from existing uppercased names, adding an ID suffix where needed for uniqueness. Existing part IDs, names, descriptions, code mappings, supplier items and stock ledgers are retained. The former part brand is preserved only as non-editable historical data (`legacy_brand`) and is not used for matching or exposed in the SKU API.

The earlier `Alternate` table represented compatibility links between different SKUs. It is retained solely for historical data, without active management or public API exposure; those links are not converted into supplier codes or used to combine SKUs.

Excel browser tests create `<admin username>-EXCEL-NEW` and `<admin username>-EXCEL-EXISTING`, check validation errors, template download, no writes during preview, repeated-code grouping, preservation of existing details/IDs, and importing the same file again. Remove these disposable SKUs and their alternos with the other management fixtures after testing.

## Validation

```sh
DJANGO_DEBUG=1 python manage.py test
DJANGO_DEBUG=1 python manage.py check
DJANGO_DEBUG=1 python manage.py spectacular --file /tmp/partsmall-schema.yml --validate --fail-on-warn
```

The OpenAPI schema must generate with zero warnings and zero errors; `mall.test_api_schema` fails if drf-spectacular reports any, or if operationIds repeat. Document every APIView with `@extend_schema` (request, parameters, responses), give list and detail routes distinct `operation_id` values, add return type hints or `@extend_schema_field` to `SerializerMethodField`s, and name any choice set reused across serializers in `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`, with values and labels exactly matching the field.

Tests cover invitation expiration/reuse, account setup requirements, supplier isolation, idempotency, ledger balance differences, source ownership, matching conflicts, brand-neutral SKU/code matching, SKU uniqueness, shared alterno editing, preservation of legacy records, and grouped supplier offers without prices or quantities. PostgreSQL concurrency tests remain necessary before production deployment.

## Catalog images

Superusers manage a SKU gallery at **Administración → Inventario → Imágenes**. Upload multiple JPEG, PNG or WebP files (10 MB per image), choose the cover, reorder, describe and remove images. The first image is the cover; deleting it promotes the next image. Catalog cards, favorites, basket and part details use these images. SKU merges transfer the gallery while preserving its file URLs.

`PartImage` belongs to the shared `Part`, with full image, thumbnail, position, description, dimensions, size, uploader and creation date. Django decodes and validates uploads, applies EXIF orientation, strips metadata, and creates a WebP image up to 2000 px and a 480 px thumbnail. API permissions restrict all gallery mutations to active superusers.

Configure `DO_SPACES_KEY`, `DO_SPACES_SECRET`, `DO_SPACES_BUCKET`, `DO_SPACES_REGION` and `DO_SPACES_ENDPOINT` in the root `.env`. Compose passes these only to Django. `DO_SPACES_CDN_ENABLED=true` serves public product images using the bucket CDN; `DO_SPACES_CDN_ENDPOINT` optionally sets a custom HTTPS origin. The CDN must be enabled for the bucket. `DO_SPACES_MEDIA_PREFIX` defaults to `motionpartes/media`, isolating this app's UUID-based objects from other apps sharing the bucket. Credentials never enter the frontend bundle. Recreate the API service after changing these settings.

Media uses Django's separate `catalog_media` storage alias. With no Spaces configuration, local development uses `media/` (served only with `DJANGO_DEBUG=1`). Images are deleted from storage after the database deletion commits; failed cleanup is logged for operator retry. CDN caches may retain an old URL until its cache expires. Replacement uploads always receive fresh URLs.

Management endpoints under `/api/v1/management/catalog/{part_id}/images/`: GET gallery, POST multipart `file` and optional `alt_text`; PATCH `{image_id}/` to edit `alt_text`; DELETE `{image_id}/`; POST `order/` with the complete ordered `image_ids` array. Catalog and management serializers return `images` ordered with the cover first, including `url` and `thumbnail_url`.

## Next development stage

Private supplier quotations (revisions, adjustments and handshake; see **Supplier orders and deals**), private supplier pricing with the AI quotation assistant (see **Precios privados y cotización asistida**) and vehicle applications (see **Technical templates and vehicle applications**) are implemented.

Not implemented yet: stock reservation or deduction at handshake, fulfilment and partial deliveries, shared server-side baskets with retail approval (membership `permission` is enforced only by the pricing settings), notifications, ratings, CSV imports and the apiag-cloud transport adapter. `output/modelo-negocio/pendientes-produccion.txt` tracks the full production checklist.

Before implementing reservations, settle expiration, partial fulfillment and how uploads that contradict confirmed agreements are resolved. Stock deduction must also be coordinated with ERP fulfillment events to prevent decrementing a delivered quantity twice. The ledger already includes movement types for this stage, but only ingestion adjustments are currently written.


### Automatic supplier matching

`docker compose up -d` now includes the `matching` worker. Excel uploads and APIAG ingestion notify a durable database queue; stock remains an absolute snapshot with credit/debit ledger deltas. A completed catalog import or edited catalog code also schedules reconciliation of previously unresolved supplier items. No open browser is required. To run one pass manually: `docker compose exec app python -m mall.matching_runner --once --force` (add `--no-ai` to use deterministic rules only).

The worker writes the full traceback of every failed pass to its log (`docker compose logs matching`) and keeps running. It reconnects to the database after each failure and waits 5 s, doubling up to 60 s while failures repeat. While running, it touches a heartbeat file (`MATCHING_HEARTBEAT_FILE` in the container environment, default `/tmp/matching-worker.heartbeat`) after every successful loop pass and, at most every 5 s, after completed database queries within a pass. While passes keep failing, a pass's queries only count once it has run for 60 s. The Compose healthcheck runs `python -m mall.matching_runner --healthcheck`, which fails when the file is missing or older than `MATCHING_HEARTBEAT_MAX_AGE` seconds (default 600). A hung pass, a loop whose passes keep failing, or a worker that cannot reach the database therefore shows as `unhealthy` in `docker compose ps` after about 11 minutes. A failure inside a matching run is retried by the queue a minute later and leaves the worker healthy. Docker does not restart an unhealthy container on its own. Manual `--once` passes do not touch the heartbeat.

The engine builds shared code, base-family, description and token indexes over the full active catalog, independent of supplier or Excel batch boundaries. It preserves leading zeros and does not interchange O/0. Known unique codes, scoped reviewed mappings, full base codes plus identical detailed descriptions, and explicit OEM references with matching specifications can link automatically. Position, numerical suffixes, dimensions and disjoint years are not discarded to manufacture a match. Description similarity alone only retrieves candidates.

Existing canonical duplicates with identical detailed descriptions and full base-code evidence are merged through the audited catalog merge service, preserving images, supplier inventory IDs, reservations and ledger entries. Consistent unmatched supplier families can create a base parent. A changed code on an already linked supplier item cannot automatically transfer it to a different parent. Reviewed supplier-local codes are learned within that supplier. A reviewed branded code can also match another supplier when both the brand and detailed specifications are identical; unbranded local codes are not treated as universal alternos.

**Administración → Inventario → Agrupación inteligente** shows pending reviews, unmatched rows, applied decisions and dismissed proposals. Review writes validate the source and target snapshots. Rejected suggestions stay rejected while their evidence is unchanged. Approved mappings are reused for later uploads. Stock quantities do not invalidate a classification decision.

Gemini receives at most 20 ambiguous supplier cases per run with at most eight catalog candidates per case. Responses are cached with the evidence, cannot introduce arbitrary destinations, and never trigger a merge themselves. A provider outage leaves deterministic matches and stock intact. The analysis button schedules another pass and retries failed AI responses; each pass analyzes the next uncached cases. Existing catalog category/subcategory classification remains available in **Asistente IA**.

API: superuser-only `GET/POST /api/v1/management/matching/` and `POST /api/v1/management/matching/{case_id}/` with `action` (`approve` or `dismiss`), `fingerprint`, and optional `target_sku`. Decisions retain evidence and reviewer/automatic attribution in `MatchingDecision`; catalog merges retain their existing audit. The worker uses a PostgreSQL advisory lock plus a visibility lease to prevent overlapping runs, and retries failures without duplicating committed matches.


### Master references and supplier items

`Part` is the brand-neutral master SKU. `PartCode` is its approved equivalence library, independent of stock: `code`, optional reference manufacturer `brand`, `ref_type` (`unknown`, `oem`, `company`), and optional `reference_source` (catalog citation or URL). Manage this in **Inventario interno → Editar → Biblioteca de equivalencias** or **Alternos**. Existing entries retain their current associations; the migration maps legacy `alias` to `unknown` and `manufacturer` to `company`. The deprecated API `kind` is mapped to the same stored field for compatibility. Register verified equivalent references; variants, kits and shared vehicle fitment alone do not prove equivalence. If a reference is already another master SKU, review and merge the masters with **Agrupar SKU** first.

`SupplierItem` still owns the supplier's stable inventory ID, its changeable `codigo`, product brand and stock ledger. Its optional `references` array contains declarations such as `[{"code":"51712-1R000","brand":"HYUNDAI"},{"code":"51712-0U000","brand":"KIA"}]`. The reference manufacturer is independent of the supplier product brand. API omission preserves references; an explicit empty array clears them. References are uppercased and deduplicated without losing leading zeros. They never create new supplier items or publish new universal catalog codes.

Supplier Excel imports accept optional **REFERENCIAS** (also **ALTERNOS** / **CODIGOS_ALTERNOS**). Separate entries with semicolons, e.g. `HYUNDAI:51712-1R000; KIA:51712-0U000; RN11002V`. Manufacturer prefixes are optional. Omitting the column preserves prior references; a blank cell in that column clears them. The downloaded template and repair workbooks include it.

The matching worker resolves declared references against the master library, even when the master and supplier codes are unrelated. Unique references with no explicit specification contradictions can link automatically. Contradictory references, measurements, positions, kit status, or historical links require review. Complete reference bases with supplier suffixes additionally require identical detailed specifications. Existing catalog variants can likewise merge through a master's reference library; ambiguous or chained links require review. AI sees both supplier declarations and master references and only proposes ambiguous matches. Library evidence is included in review snapshots, so editing a reference invalidates a stale approval. No automatic match publishes a supplier-local code as a universal alterno. When no SKU, alterno, reviewed mapping or declared reference carries a code, the worker also reads the OEM library: an aftermarket code a catalog files under an OEM number some SKU reaches (`aftermarket_code`, matched with the supplier's brand) or that OEM number itself (`linked_oem_number`). It links automatically only when exactly one SKU qualifies, the code is printed the same way or has at least 8 characters, and no specification contradicts it; several SKUs (usually duplicates to group) go to review as candidates. Declared references use the same library when no SKU or alterno carries them, comparing an OEM reference's manufacturer by make group.

## Technical templates and vehicle applications

Each **subgrupo is a `PartType`**, with a permanent UUID and a parent grupo (`category`). Subgrupo names are unique within their grupo. `Part.part_type` selects the template; the existing `category` and `subcategory` strings remain compatible projections for catalog filters, Excel imports and AI classification. Existing complete classifications are backfilled. Imports create/reuse types in batches, and incomplete classifications remain untyped. Renaming a type through the management API updates its items without changing IDs, values or template ownership.

In **Administración → Plantillas técnicas**, create a subgroup and configure its `TechnicalTemplate`. `TechnicalField` defines a stable key, Spanish label, display section/order, type, canonical unit, selection options and whether the field is needed for completeness. The SKU's **Ficha técnica** action renders these fields dynamically. Supported types are decimal measurement, integer, boolean, selection and text. A missing value remains unknown; `false` and zero are real values. Required fields report an incomplete sheet without preventing an administrator from saving partial work.

`PartSpecification` holds one typed value per SKU and template field, plus an internal source reference. Measurements are stored as decimals, not arbitrary strings. Supported same-dimension input units are converted to the template's unit (for example, 25.6 cm → 256 mm); incompatible units, nonfinite values, fractional integers and values requiring more than six decimal places are rejected. Numeric values are indexed by field for future range filtering. Customer measurement search/filter controls are not enabled in this version.

Templates and item sheets use revision checks to reject stale writes. An in-use field cannot be removed or change its key, type or unit; in-use selection options must be retained. An item with specification values cannot move to another subgroup until its sheet is explicitly cleared. SKU merges that would retire a source with specifications or applications are held for review, rather than dropping its sheet or extending vehicle compatibility without evidence. These master-SKU values must apply to its equivalent products; product-specific variations still need their own future product/variant records.

`Application` is a reusable vehicle configuration: make, model, generation/chassis, year range, engine, trim, transmission and market. Empty optional fields mean unspecified, not universal fitment. `ItemApplication` is the explicit many-to-many join from `Part` to `Application`; each link has installation position, fitment notes, source and verification state. The same application can be linked to many items, and one item can link to many applications. Repeating an application/position pair is rejected. Application identities already used by items cannot be edited or deleted; create a new configuration and change the intended links instead. Only verified links appear in the customer's **Ficha técnica y aplicaciones** section, and internal sources are omitted.

All writes and management reads below require a superuser. Catalog sheets require authentication and an active account membership, matching catalog access. URLs below are Django paths; the frontend proxies use `/api/management/...` and `/api/market/...` respectively, without trailing slashes.

| Query or operation | Endpoint |
| --- | --- |
| Find/create subgrupos | `GET/POST /api/v1/management/part-types/` (`search`, `page`; ordered by group, subgrupo and ID) |
| Rename a subgrupo/group | `GET/PATCH /api/v1/management/part-types/<type_id>/` |
| Fetch/configure its template | `GET/PATCH /api/v1/management/part-types/<type_id>/template/` |
| Resolve a SKU's template and edit its values/applications | `GET/PATCH /api/v1/management/catalog/<part_id>/technical/` |
| Find/create vehicle configurations | `GET/POST /api/v1/management/applications/` (`search`, `page`) |
| Edit/delete an unused configuration | `GET/PATCH/DELETE /api/v1/management/applications/<application_id>/` |
| Customer sheet and verified vehicle applications | `GET /api/v1/catalog/<part_id>/technical/` |

The catalog management API also accepts a `part_type` UUID when creating/editing a SKU and fills its grupo/subgrupo. Templates are queried through that relationship, never selected by an item's measurements. A sheet GET returns `part_type`, `template` (including its revision and fields), `revision`, `values`, `applications` and `missing_required`. For example:

```json
{
  "revision": 0,
  "part_type": "<subgrupo UUID from GET>",
  "template_revision": 2,
  "values": {
    "diameter": {"value": "25.6", "unit": "cm", "source": "Manufacturer technical sheet"},
    "abs": {"value": false}
  },
  "applications": [
    {"application": "<application UUID>", "position": "DELANTERO", "notes": "SOLO CON ABS", "source": "Fitment catalog", "verified": true}
  ]
}
```

A supplied `values` object replaces the specification set; a supplied `applications` list replaces the linked applications. Omitted sections remain unchanged. Send `{}` or `[]` explicitly to clear a section. Template PATCH submits its current `revision` and complete ordered `fields` array. Definitions are returned by the template endpoint and documented in `/api/docs/`.

Validation coverage lives in `mall/test_technical_data.py`. `frontend/tests/technical-data.spec.ts` exercises subgroup creation, template configuration, reusable application creation, typed values, verification, reload persistence and mobile layout using disposable `E2E_TECHNICAL_TOKEN` and `E2E_TECHNICAL_RUN` fixtures.

### Fast category suggestions

The inventory assistant defaults to **Categorías · rápido y económico**. It processes 200 source items per saved batch and reads only those items. The existing **Categorías y equivalencias** mode and saved jobs keep their original 50-item batches and merge-evidence checks.

Category-only suggestions use anchored Spanish/ERP description rules (for example `TACO HYU` → `FRENOS / PASTILLAS DE FRENO`, while `CLIP TACO` is an accessory). Ambiguous terms, conflicting existing classifications, and non-brake `PASTILLAS` descriptions are not handled by broad keyword matching. Remaining descriptions are deduplicated exactly after case/accent/whitespace normalization, retaining vehicle, position, dimensions and current classifications. Gemini receives indexed descriptions and a controlled taxonomy, and returns only category indices/confidence. No SKU equivalence context is sent. Unknown types remain for individual review.

Validated AI suggestions with confidence >=85% can be reused for 30 days. Cache identity includes description, existing classification, instructions, model, taxonomy and rules version. Correcting or dismissing a proposal invalidates its cached result. Custom instructions bypass local rules, so they may use more tokens. Local suggestions work without an API key. Usage counters report rules, cache reuse, AI descriptions and provider-reported tokens for successfully saved batches; they are not a billing ledger for failed/abandoned requests.

`POST /api/v1/management/catalog/assistant/` accepts `task: "categories"` (API omission retains legacy `grouping`). An explicit `apply_category` action with `category` and `subcategory` applies up to 200 pending proposals of that type with confidence >=85%, preserving optimistic source checks and updating PartType/template links in bulk. Repeat until the category has no pending eligible proposals. Lower-confidence proposals require individual review. Classification alone never changes catalog items, stock, vehicle fitments or equivalence links.


### OEM-preferred MAIN and company references

Reference type and manufacturer are separate: `{"code":"0110-GSU45A48","ref_type":"company","brand":"FEBEST"}` is a company code; `ref_type: "oem"` identifies an original-equipment reference and `brand` identifies its manufacturer. A supplier is not the reference manufacturer. Unknown codes remain `unknown`; stripping a supplier suffix never proves an OEM identity.

`Part.is_OEM` is a persisted boolean on the **main SKU**, exposed by catalog APIs and editable in **Editar SKU → EL SKU PRINCIPAL ES OEM**. It defaults to `false` (internal/company/unverified); `true` explicitly identifies that main code as OEM. It is independent of each alterno's `ref_type`. Migration `0026` only marks existing mains that match an OEM reference with manufacturer and source; it does not guess from code patterns. Verified OEM promotions set the flag automatically. An already marked main is retained unless a different verified OEM is explicitly selected. Catalog PATCH resets the flag on a changed SKU unless `is_OEM` is supplied for the new code. Admin catalog lists support `?is_OEM=true` or `false`, with the corresponding inventory filter. OEM flags participate in matching/review snapshots and prevent suffix-based stripping of an approved OEM.

The matching worker and catalog editor prefer an approved OEM that has both a manufacturer and a nonempty source citation. They retain an existing verified OEM MAIN; otherwise a sole OEM can become MAIN automatically. Multiple OEM codes require selection in **Inventario interno → Editar → OEM para el MAIN** (`main_reference: {code, brand}` on catalog POST/PATCH). The operation keeps the same Part UUID, specifications, fitments, images, supplier inventory identities and ledger, retains the old SKU as an alterno, and records `CatalogIdentityChange`. Conflicting catalog ownership requires grouping review. When the verified OEM already has a master, reference reconciliation prefers that master. Ordinary unknown SKU renames are not evidence of OEM equivalence.

An explicit deployment-level `CATALOG_COMPANY_SUFFIXES` registry identifies known company suffixes; the initial mappings are `FEB`/`FEBEST` → `FEBEST`. These rules preserve the full supplier code and the manufacturer code as company references, without manufacturing OEM mappings. The worker applies them across uploads, rather than maintaining a special case for one item.

**OEM finder (dry run).** `docker compose exec -T app python -m mall.oem_finder --dry-run [--in-stock-first] [--limit N] [--tier T]` grades every active non-OEM SKU into the validated OEM tiers (AUTO_FLAG_CURRENT, AUTO_RENAME_BASE, review tiers, NEEDS_AI, SKIP...) from the SKU's OEM format, the suffix table, a class lexicon built from the catalog itself, supplier codes and alternos. Only non-OEM SKUs are graded, but the learned indexes (model hints, class lexicon, sibling base claims and claimants) learn from every active canonical SKU, OEM-flagged ones included, so applying a row never moves its neighbours (rules `oem-finder-3`); a graded row never counts its own key. It takes the matching worker's advisory lock (exit code 2 while a matching pass runs), records an `OEMFinderRun` (rules and suffix-table versions, scope, tier counts, timings) and syncs the review tiers into `OEMReviewCase` (one per Part, with the proposed MAIN in the manufacturer's form, the written form, blockers and evidence). Re-running is idempotent: a case whose fingerprint is unchanged keeps its decision, changed evidence reopens it, and a Part that left the review tiers is resolved. The fingerprint covers the proposal (snapshot, candidate, tier, blockers, suffix entries), not the rules version: after a rules bump an unchanged proposal gets the new evidence and keeps its status and decision (*restamped*). Tiers are always computed over the whole catalog; `--limit`/`--tier` only narrow the cases it syncs. It never renames a SKU, never writes a PartCode (a verified OEM PartCode would be promoted by reference reconciliation) and never calls AI. `--read-only` writes nothing and works before migrating (otherwise the command exits 1 until migration `0035_oem_finder` is applied); `--json PATH` (or `-`) exports the selected rows. `OEM_FINDER_AUTO_STAGE=1` also runs it as the worker stage `oem` after catalog grouping (off by default).

**OEM finder (auto-apply and undo).** `docker compose exec -T app python -m mall.oem_finder --apply-auto [--tier AUTO_FLAG_CURRENT|AUTO_RENAME_BASE|STRONG_PENDING_OWNER_TAGS] [--limit N] [--canary 50] [--spot-check-dir DIR]` applies only the AUTO tiers: SKUs that already are the OEM number are marked OEM (a verified `PartCode` with the make as brand and `reference_source='algo:oem-finder:v3:current:<run>'`, then `prefer_oem(confirm_current)`), and OEM base + tag SKUs are renamed to the manufacturer's form of the base (`...:strong-<tags>:<run>`), keeping the same Part UUID, stock, ledger, photos, specifications and alternos, with the old SKU kept as an alterno. `STRONG_PENDING_OWNER_TAGS` selects the renames that rest on an owner-confirmed tag (G, NP); rows still waiting for a confirmation are never applied. Rows go in stock first, in batches of at most 100, each in its own transaction (supplier accounts, then the Part, locked), re-checked against the run's snapshot (SKU, description, taxonomy, OEM flag, codes and no new SKU, alterno or linked supplier code claiming the number); a changed row is skipped and re-evaluated by the next run. `prefer_oem` now compares references by `normalized_reference` (`MR-968365` = `MR968365` = `MR968365_`) on the new MAIN and on the old SKU, across every Part SKU (retired included, except SKUs already grouped into the same Part, which only block an identical MAIN) and every alterno; a refusal turns the row into a `CONFLICT` review case (merge review), never a retry, and the dry run keeps that case open. Each tier needs a completed `--canary N` run of the current rules version before batches (exit 1 otherwise; a rules bump such as `oem-finder-3` needs new canaries); every run stores a spot-check sample of 20 random applied rows with their evidence, downloadable by superusers as JSON or CSV (`GET /api/v1/management/oem-finder/runs/<id>/spot-check/?download=csv`). The halt rule is an ops flag (`--halt "motivo"` / `--resume`, or `POST /api/v1/management/oem-finder/halt/`): while it is active no run starts (exit 3) and a running one stops before its next batch; unexpected errors (or a failed run) stop the run and raise it automatically. Every applied row writes a `CatalogIdentityChange` linked to its run, method, evidence and undo data in the `OEMFinderChange` side table. `python -m mall.oem_finder_revert --run <id> [--part <uuid>]` (or `POST /api/v1/management/oem-finder/runs/<id>/revert/`, or **Inventario → Aplicación OEM**) restores the old SKU when no other SKU holds it, deletes the PartCodes the run created (the OEM reference and the alternos the rename kept) and restores any it retyped, clears the OEM flag, writes a reverse `CatalogIdentityChange` and stamps `reverted_at`; it is all or nothing per SKU (a SKU renamed, grouped or whose run-written codes were edited since is reported and left untouched), repeating it changes nothing, a reverted OEM is never re-applied automatically (nor one a person dismissed for that SKU in **Revisión OEM**, even when new rules or evidence grade it AUTO later), and a fully reverted canary no longer unlocks batches (run a new canary). `--apply-auto --read-only` previews the run (per tier: selected, would apply, conflicts) without writing anything and works before migrating; applying needs migration `0036_oem_finder_apply`. Reference reconciliation now skips Parts already marked OEM.

**OEM finder (pending auto rows).** Every dry run (command or worker stage) and every completed apply run refreshes `OEMAutoCandidate` (migration `0038_oem_auto_candidates`, a new table; the first dry run after migrating fills it): one row per SKU graded `AUTO_FLAG_CURRENT` or `AUTO_RENAME_BASE`, with the apply tier `mall.oem_apply` would use (`AUTO_RENAME_BASE`, or `STRONG_PENDING_OWNER_TAGS` when the rename rests on an owner-confirmed tag such as `-G`), the proposed manufacturer-form OEM, make, suffix chain with classes, grade, evidence, stock and fingerprint. A row is `pending` while the next apply run of its tier would take it, `applied` once its change stands, `excluded` while O3 leaves it to a human (reverted before with the same OEM, an open apply conflict, or the same OEM dismissed in **Revisión OEM**; a revert excludes it at once and it never returns as automatic), `stale` once it no longer grades AUTO, and `sent_to_review` when a superuser takes it out of automatic application: `apply_auto` then skips it and an `OEMReviewCase` (CURRENT_REVIEW, PROBABLE_BASE or STRONG_PENDING_OWNER_TAGS with blocker `sent_to_review`, created or reopened) lets a person decide in **Revisión OEM**. A run that wrote something grades the catalog again before refreshing (a rename changes the SKU its neighbours' claims read; a flagged SKU keeps teaching the lexicon). After a rules bump the next run refreshes every graded row (`rules_version`) without changing `sent_to_review`, `applied` or `excluded`, and a reverted OEM stays excluded across versions. Superuser API: `GET /api/v1/management/oem-finder/pending/` (filters `tier`, `apply_tier`, `chain`, `make`, `system`, `in_stock`, `search`, `status`, `ordering`; counts per tab, facets, canary credit per apply tier for the current `rules_version`, halt and lock state), `POST .../pending/preview/` (`{tier, mode: canary|batch}`: a read-only `apply_auto` listing the exact rows a canary of 50 or a batch of 100 would take and what the catalog would say), `POST .../pending/apply/` (`{tier, mode, operation_id}`: `apply_auto` itself under the matching advisory lock; 409 while halted, locked, importing, for a batch without a canary of that apply tier, or for an operation id reused for another apply; repeating an operation id returns its run without applying again) and `POST .../pending/send-to-review/` (`{parts: [uuid]}`, up to 100). **Inventario → Aplicación OEM → Pendientes de aplicación automática** shows the tabs *SKU YA ES EL OEM* and *RENOMBRAR A BASE OEM*, preview, canary and batch buttons with a confirmation, the result linked to the run history (spot-check and undo), and per-row or selected *Enviar a revisión*. Endpoints answer 503 until migration 0038 is applied.

**Buscar referencias OEM con IA** calls superuser-only `POST /api/v1/management/catalog/{part_id}/oem-lookup/`, optionally with `company` and `code`. One grounded Google Search call researches the exact code and one compact extraction call structures the results. Cached results (model/code/company/description/classification/version) are reused for 30 days. No whole-catalog web research is launched. An ungrounded response is rejected and remains retryable. Results show source links and distinguish possible identical-part references from containing assemblies and uncertain references. Only reviewed possible equivalences can be added to the editor; saving approves them. Research alone never changes Parts, PartCodes, matches or stock. An OEM number for a complete assembly must not be used as an interchangeable code for its individual component.

Supplier `references` also accept optional `ref_type`, with company references requiring `brand`. Typed matching respects the reference namespace. Excel's existing **REFERENCIAS** column accepts `OEM:TOYOTA:43460-EXAMPLE; COMPANY:FEBEST:0110-GSU45A48`; legacy `BRAND:CODE` and plain codes continue to work. Supplier declarations remain matching evidence, not automatically approved universal equivalences.

**Analizar catálogo y proveedores** reconciles the existing reference library, catalog families and unresolved supplier items. It does not launch web research or category classification. Its panel shows queued/running/completed/retry states, the current phase, catalog size, already-linked supplier items, examined pending items, changes and AI usage. No eligible ambiguous cases means no AI call. The final summary distinguishes an unused AI from an unavailable or failed provider and shows marked/unmarked OEM main counts. Applied/dismissed tabs are cumulative history, not changes from the last run. OEM web research remains the separate per-item action above, with cached results and reviewed equivalences.

### Supplier PDF catalogs

Supplier catalogs in PDF (kept locally in `pdf_catalogues/`, which is git-ignored) are read without AI: each catalog layout gets a small deterministic parser that writes a normalized JSON extract (catalog, then items with the brand's code, product type, printed OEM numbers, competitor cross-references, vehicle applications and printed measurements). `python -m mall.supplier_catalog_import --dry-run|--apply FILE... [--report PATH]` loads those extracts:

- Every printed OEM number becomes, or joins, a row of the OEM table with one *aftermarket catalog* source per catalog (status *declarado*), carried by a SKU or not.
- A catalog item links to a SKU only when one of its numbers equals a code of that SKU (the SKU, its OEM base after suffix stripping, its alternos or its supplier codes), the SKU description classifies to the item's product type with the deterministic category rules (plus importer rules for hubs, wheel and tensioner bearings, belt tensioner pulleys, fan clutches and wear sensors), and the makes agree. Makes are compared through the OEM finder's make groups (VOLKSWAGEN, AUDI and SEAT meet VAG; MERCEDES-BENZ meets MB; JEEP and DODGE meet MOPAR); the finder's OTHER bucket counts as no make evidence, so only an OEM-number match links there. SKUs retired with `-ANULADO` or `_DELETED` never match, and alternos a supplier catalog wrote (they cite it) never act as matching keys, so a re-run or the next catalog cannot chain one catalog's competitor codes into new links. A SKU matching several items of one product type links to all of them only through an OEM number they all print (a shared competitor code is no proof); items matching several SKUs are reported as merge candidates for Agrupar SKU.
- Linked SKUs receive the catalog's own code and its competitor codes as company alternos, citing the catalog page, only when no OEM number carries those codes to the SKU (an item that prints no usable OEM number); otherwise the SKU reaches them through its numbers (see step 2c below). OEM numbers become OEM alternos only on SKUs already marked OEM: on any other SKU the matching worker's reconciliation would promote a sole sourced OEM alterno to MAIN outside the OEM finder's canary and review.
- When an item prints measurements, an unclassified linked SKU gets the subgrupo its own description names (one the item's product type accepts, else the item's), its template gains the missing fields and the SKU gets the values (source: catalog and page). Codes printed in measurement columns (boot codes, brake system) stay text; remarks and the brand's own short numbers are not measurements and are skipped; values outside a plausible range for the field and product type (a 630 mm CV seal) are reported, never stored.
- Nothing is overwritten: codes owned by another SKU, different stored measurements and other subgrupos are reported and left alone; re-running a file changes nothing, and a dry run compares with the stored values so it reports exactly what an apply would add. Apply runs take the matching worker's lock; back up the database first.

**Aftermarket codes filed under the OEM number (alternos, step 2a).** The same import also files every aftermarket code an item prints (the catalog's own code and the competitor codes) under each OEM number of the item as an `OEMCrossReference`, whether or not a SKU carries the number. The codes belong to the number, not to a SKU: duplicate SKUs share them and grouping SKUs moves nothing. A SKU reaches them through its OEM numbers: its OEM alternos, the number its own code names (`16100-79445-NPW` → TOYOTA 1610079445: the code before trailing brand or neutral tags, never a variant; a number filed under several makes links under each unless the SKU's description names another make) and the numbers a catalog printed for the item matched to it (`PartOEMLink`, sources `sku_name` and `catalog`). A link never makes the number the SKU's MAIN. A number a catalog prints for two or more different parts (variants such as GWT-116A and GWT-116AH count as one) files no codes from that catalog and ties no SKU, because it cannot say which part replaces it: an inner and an outer joint under one drive-shaft number, or a row the extract misread (GMB prints 16100-79445 for three pumps). A SKU matched to an item only through such numbers gets no catalog link; the dry run counts these matches and lists samples (`shared_number_matches`), since the company alternos written for them earlier deserve a review. Assembly numbers listed for a CV component file nothing. **Biblioteca OEM** shows each number's codes and the SKUs linked by name or catalog; **Editar SKU** shows the SKU's numbers and codes read-only. Saving a SKU refreshes its own name links, an apply of the catalog import refreshes all of them, and `python -m mall.link_oem_numbers --dry-run|--apply` does it on demand (for example after a suffix-table edit). These rows are derived from the extracts and the importer only adds to them: after changing these rules, empty `OEMCrossReference` and the `catalog` links and re-run every extract. The company alternos already on the SKUs stay where they are.

**Codes reached through the OEM number replace the copies (alternos, step 2c).** The catalog import no longer copies an item's codes onto a SKU when an OEM number carries them to it, and writes nothing for a SKU matched only through numbers the catalog prints for several parts. `python -m mall.prune_catalog_alternos --dry-run|--apply [--report PATH]` removes the company alternos a catalog copied earlier once the SKU reaches the same code through its numbers (1,319 on 302 SKUs when it first ran). A SKU carrying any catalog alterno it cannot reach that way keeps all of them for review (55 SKUs, 276 alternos); hand-typed alternos, OEM alternos and codes no imported catalog cites are never touched. The report lists every alterno removed or kept, apply runs take the matching worker's lock, and a re-run removes nothing more. Customers see a SKU's own alternos plus the codes it reaches through its numbers (`equivalents` in the catalog and wishlist payloads) on the cards, the product detail and the basket; searches and supplier matching find SKUs through those codes (step 2b). A supplier upload carrying such a code is matched by the worker moments later instead of instantly. **Alternos → Por revisar** (`?seccion=alternos&vista=revisar`) lists the SKUs the cleanup leaves whole: for each one, its OEM numbers with the catalog items that print each (a number printed for several pieces is flagged), and every catalog alterno no number of the SKU carries, with the numbers a catalog files that code under and their applications. *Conservar* keeps the code as a confirmed alterno (its source becomes `CONFIRMADO POR <USUARIO> · <cita>`, so the cleanup never treats it as a copy again), *Retirar* deletes it, and *Retirar copias* deletes the SKU's catalog alternos it already reaches through its numbers. Superuser API: `GET /api/v1/management/alternate-review/`, `GET|POST .../alternate-review/<part_id>/` (`{action: keep|remove|remove_copies, alternate}`; 409 with the current state when the alterno is no longer to decide).
