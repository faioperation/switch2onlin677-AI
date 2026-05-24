# Project Structure — DhifafBot AI Service

```
AI/
├── main.py                        App entry-point: FastAPI setup, router registration,
│                                  scheduler, chat pipeline, knowledge/rate/prompt endpoints.
│
├── database.py                    SQLAlchemy engine, SessionLocal, Base, get_db dependency.
├── models.py                      All ORM models (Product, Brand, Category, ChatHistory,
│                                  Bundle, BundleItem, ProductStatusLog, KnowledgeChunk …)
├── tools.py                       AI pipeline tool functions: search_products,
│                                  get_product_details, check_availability, format_products.
│
├── core/                          App-wide shared infrastructure.
│   ├── __init__.py
│   ├── config.py                  settings singleton: all env vars, file paths, constants.
│   │                              Import: from core.config import settings
│   ├── exceptions.py              Typed exception hierarchy:
│   │                              AppError, NotFoundError, ConflictError,
│   │                              AppValidationError, ForbiddenError, ServiceError.
│   │                              Global handlers in main.py convert these to JSON.
│   └── recommendation_context.py  RecommendationContext (frozen dataclass):
│                                  user_id, session_id, preferred_*, viewed_barcodes,
│                                  cart_barcodes, locale, is_bot.
│                                  Factory: from_params(), anonymous().
│
├── services/                      Business logic & external integrations.
│   ├── __init__.py
│   ├── sync.py                    SAP bi-daily price/stock sync (async).
│   │                              Entry: sync_sap_data()
│   ├── upload.py                  Bulk CSV/XLSX product upsert.
│   │                              Entry: upsert_product_upload(db, filename, content, dry_run)
│   ├── recommendation.py          Curated recommendation queries (8 functions).
│   │                              Entries: get_best_selling, get_new_arrivals, get_recommended,
│   │                                       get_cod_recommended, get_by_price_tier,
│   │                                       get_by_brand_family, get_bundle, get_featured
│   ├── scoring.py                 Recommendation scoring pipeline (Step 13).
│   │                              Scorer Protocol, RuleBasedScorer, PersonalizationScorer,
│   │                              ScoringPipeline, get_active_scorer().
│   │                              Env: RECOMMENDATION_SCORER=rule_based|personalization
│   ├── bundle.py                  Bundle service — reads bundles + bundle_items tables.
│   │                              Entries: list_bundles, get_bundle_detail,
│   │                                       get_bundles_for_product
│   └── status.py                  Product status management (Step 15).
│                                  ProductStatusService: change_status, bulk_change_status,
│                                  update_flags, bulk_update_flags.
│                                  Enforces transition matrix; writes ProductStatusLog.
│
├── repositories/                  Data-access layer — all raw DB queries live here.
│   ├── __init__.py
│   └── product.py                 ProductRepository(db): get_by_barcode, list_products,
│                                  update, delete, get_filter_options.
│                                  No HTTP concerns; raises AppError subclasses.
│
├── routers/                       HTTP layer — thin FastAPI routers.
│   ├── products.py                9 endpoints:
│   │                                GET    /products/filters
│   │                                GET    /products
│   │                                GET    /products/{barcode}
│   │                                PUT    /products/{barcode}
│   │                                DELETE /products/{barcode}
│   │                                PATCH  /products/{barcode}/status  ← Step 15
│   │                                PATCH  /products/{barcode}/flags   ← Step 15
│   │                                POST   /products/bulk/status       ← Step 15
│   │                                POST   /products/bulk/flags        ← Step 15
│   ├── categories.py              CRUD /categories
│   ├── brands.py                  CRUD /brands
│   ├── subcategories.py           CRUD /subcategories
│   ├── recommendations.py         GET /recommendations/* (8 endpoints)
│   │                              All endpoints accept scoring context params:
│   │                              user_id, session_id, viewed_barcodes, cart_barcodes, locale
│   └── bundles.py                 3 endpoints:            ← Step 14
│                                    GET /bundles
│                                    GET /bundles/{bundle_code}
│                                    GET /bundles/for-product/{barcode}
│
├── schemas/                       Pydantic v2 request/response models.
│   ├── __init__.py
│   ├── product.py                 ProductResponse, ProductUpdateRequest,
│   │                              PriceTierEnum, ProductStatusEnum
│   ├── upload.py                  ProductUploadRow, UploadResult, UploadRowError
│   └── status.py                  StatusChangeRequest, BulkStatusChangeRequest,
│                                  FlagsUpdateRequest, BulkFlagsUpdateRequest
│
├── alembic/                       Database migrations (Alembic).
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 20260517_013065484c78_add_knowledge_chunks_table.py
│       ├── 20260519_001_nullable_item_code_and_item_name.py
│       ├── 20260522_001_extend_product_fields.py
│       ├── 20260525_001_add_missing_performance_indexes.py  ← Step 12
│       ├── 20260525_002_add_bundle_tables.py                ← Step 14
│       └── 20260525_003_add_product_status_log.py          ← Step 15
│
├── static/                        Frontend assets (index.html, JS, CSS).
├── knowledge_base/                Uploaded PDF/TXT knowledge files + index.json.
│
├── sync_service.py                ← SHIM — re-exports from services/sync.py
├── recommendation_service.py      ← SHIM — re-exports from services/recommendation.py
├── product_upload_service.py      ← SHIM — re-exports from services/upload.py
│
├── system_prompt.txt              Editable AI system prompt (loaded at runtime).
├── rate.json                      IQD exchange rate (updated via POST /rate).
├── leads.json                     Local leads cache.
├── sap_sync.log                   SAP sync run log.
├── .env                           Environment variables (never committed).
├── requirements.txt
└── STRUCTURE.md                   ← this file
```

---

## Layer responsibilities

| Layer | Folder | Rule |
|---|---|---|
| **HTTP** | `routers/` | Parse request → call service/repo → return response. No DB queries. |
| **Business logic** | `services/` | Orchestrate DB + external APIs. No HTTP types (no Request/Response). |
| **Data access** | `repositories/` | All ORM queries. One Session injected, never opened internally. |
| **Config** | `core/config.py` | All `os.getenv()` calls centralised here. Never read env vars elsewhere. |
| **Exceptions** | `core/exceptions.py` | All custom exceptions live here. Routers raise them; global handlers convert to JSON. |
| **Schema** | `schemas/` | Input validation and output shaping only. No DB imports. |

---

## Import rules

```
routers → services, repositories, schemas, core.*
services → models, repositories, tools, core.*
repositories → models, core.*
schemas → (nothing from this project)
core → (nothing from this project)
main.py → routers, services, tools, database, models, core.*
```

Circular imports are prevented by this hierarchy.  
`tools.py` and `database.py` remain at root because they are imported by
many layers and moving them would require updating dozens of paths with
no structural benefit.

---

## Backward-compatibility shims

`sync_service.py`, `recommendation_service.py`, and `product_upload_service.py`
at the project root are **thin re-export shims** — they exist only so any
external script or server config that imports from the old path still works.
All real code lives in `services/`. The shims can be deleted once all callers
have been updated to the new paths.

---

## Recommendation engine evolution path

| Phase | Scorer | Status |
|---|---|---|
| 1 (now) | `RuleBasedScorer` | Live — trusts DB ordering, adds `scoring` metadata block |
| 2 | `PersonalizationScorer` diversity | Live — filters viewed/cart barcodes |
| 2+ | `PersonalizationScorer` ML | Stub — logs debug, falls back to editorial |
| 3 | Hybrid blend | Planned — α × editorial + (1-α) × ML score |
| 4 | A/B via user_id hash | Planned — `ScoringPipeline` picks scorer by hash |

Activate PersonalizationScorer: `RECOMMENDATION_SCORER=personalization` in `.env`

---

## Product status transition matrix

```
              → draft   → active   → inactive
  draft         —          ✓           ✓
  active        ✓          —           ✓
  inactive      ✗          ✓           —
```

`inactive → draft` is blocked: re-activate the product first (`inactive → active`),
then pull back to draft (`active → draft`) if editing is needed.

Every transition writes an immutable row to `product_status_log`
with `from_status`, `to_status`, `changed_by`, `reason`, and `changed_at`.
