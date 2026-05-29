# Switch2Online AI — Product Upload & AI Recommendation API Documentation

> **Version:** 1.0.0 — Generated from source code on 2026-05-30  
> **Base URL:** `http://<host>:<port>`  
> **Content-Type:** `application/json` (all endpoints except file upload)  
> **Authentication:** None required on any endpoint in this version  

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Architecture Overview](#2-architecture-overview)
3. [Product Upload APIs](#3-product-upload-apis)
4. [Product Management APIs](#4-product-management-apis)
5. [Health & Monitoring APIs](#5-health--monitoring-apis)
6. [AI Recommendation APIs](#6-ai-recommendation-apis)
7. [AI Embedding APIs](#7-ai-embedding-apis)
8. [Behavioral Tracking APIs](#8-behavioral-tracking-apis)
9. [Data Models](#9-data-models)
10. [Recommendation Scoring](#10-recommendation-scoring)
11. [Error Reference](#11-error-reference)
12. [Deployment Guide](#12-deployment-guide)
13. [Operational Guide](#13-operational-guide)

---

## 1. Introduction

The Switch2Online AI service is a FastAPI-based backend that powers:

- **Product catalog management** — bulk Excel/CSV upload, status management, editorial flags
- **AI-powered recommendations** — semantic search, similarity, cross-sell, upsell, skincare, fragrance
- **Embedding pipeline** — OpenAI `text-embedding-3-small` (1536 dimensions) via pgvector
- **Behavioral feedback loop** — event tracking and user preference learning
- **SAP integration** — price and stock data synchronized twice daily

### Standard Response Envelope

Every endpoint returns a consistent JSON envelope:

**Success:**
```json
{
  "success": true,
  "data": { ... }
}
```

**Error:**
```json
{
  "success": false,
  "error": "Human-readable error message."
}
```

---

## 2. Architecture Overview

### 2.1 Product Upload Flow

```
POST /products/upload
        │
        ├─ 1. File-level validation (size, MIME, extension, corruption check)
        │       → 400 if invalid
        │
        ├─ 2. create_upload_job() → writes UploadJob row (status=queued)
        │       → returns job_id immediately (202 Accepted)
        │
        └─ 3. FastAPI BackgroundTask: process_upload_job()
                │
                ├─ Parse file → DataFrame (pandas)
                ├─ Validate columns (barcode required)
                ├─ Pydantic-validate ALL rows → split valid / invalid
                ├─ preload_entity_caches() → 3 DB queries (brands, categories, subcategories)
                ├─ create_missing_entities() → auto-creates unknown brands/cats/subcats
                └─ For each batch of 500 rows:
                        ├─ One IN query to find existing barcodes
                        ├─ bulk_insert for new products
                        ├─ bulk_update for existing products
                        ├─ upsert ProductSearchIndex
                        ├─ commit batch + update job progress
                        └─ (dry_run=true → rollback at the end)
```

**Performance:** Old implementation used ~6 DB queries per row (60,000 for 10K rows). New implementation uses ~10 DB queries total regardless of file size.

### 2.2 Embedding Pipeline

```
Daily at 03:00 IQ (Baghdad) or on-demand via POST /ai/embeddings/trigger
        │
        ├─ Query: products WHERE embedding IS NULL (or force_all=true)
        ├─ Preload ProductSearchIndex for name resolution (1 query)
        └─ For each batch of 50 products:
                ├─ build_embedding_text() → structured prose block:
                │     "Product: {name}\nBrand: {brand}\nCategory: {cat}..."
                ├─ OpenAI Embeddings API: text-embedding-3-small (1536 dims)
                ├─ Persist embedding + embedding_text + embedding_updated_at
                ├─ Compute base ai_score from editorial + stock + freshness signals
                ├─ db.commit()
                └─ sleep 0.5s (rate limit buffer)
```

### 2.3 Semantic Search Flow

```
User Query
        │
        ├─ embed_single(query) → OpenAI embedding vector
        │
        └─ pgvector cosine distance query:
                SELECT ... FROM products p
                LEFT JOIN productsearchindex psi ON psi.product_id = p.barcode
                WHERE p.embedding IS NOT NULL
                  AND p.product_status = 'active'
                  AND p.available_qty > 5
                  AND p.price > 0
                ORDER BY p.embedding <=> query_vector
                LIMIT candidates
                │
                └─ Python-side hybrid re-ranking:
                        final_score = W_SEM×semantic + W_ED×editorial
                                    + W_POP×popularity + W_STK×stock + W_FRESH×freshness
                        Sort by final_score DESC → return top limit
```

### 2.4 Hybrid Recommendation Engine

The engine operates in three modes selected by `RECOMMENDATION_SCORER` env var:

| Mode | Env Value | When to Use |
|---|---|---|
| Rule-Based | `rule_based` (default) | 0% embedding coverage; trusts DB editorial ordering |
| Personalization | `personalization` | Session diversity + cart exclusion; ML placeholder |
| Hybrid AI | `hybrid_ai` | ≥50% embedding coverage; blends ai_score + editorial |

**HybridAIScorer formula:**
```
final = SCORE_ALPHA × ai_score + (1 - SCORE_ALPHA) × editorial_score
```
Default `SCORE_ALPHA = 0.55` (AI-weighted, configurable via env var).

### 2.5 User Preference Learning Flow

```
POST /events/product/{barcode} (event_type = click | purchase | recommendation_accepted)
        │
        └─ Background task: update_user_preference_profile()
                │
                ├─ Fetch product.embedding
                ├─ Load UserPreferenceProfile (or create for cold-start)
                ├─ Exponential moving average:
                │     new_embedding = (1 - α) × old_embedding + α × product_embedding
                │     α = 0.2 × event_weight
                ├─ Normalize to unit length
                ├─ Update preferred_categories / brands / price_tiers / skin_types frequency maps
                └─ db.commit()

Event weights:
  purchase:                1.0
  recommendation_accepted: 0.9
  click:                   0.7
  view:                    0.3 (view events do NOT trigger profile update)
```

---

## 3. Product Upload APIs

### 3.1 POST /products/upload

**Purpose:** Accept an Excel (.xlsx) or CSV (.csv) file, validate it, create an upload job, and process it asynchronously.

**HTTP Method:** `POST`  
**URL:** `/products/upload`  
**Content-Type:** `multipart/form-data`

#### Request

| Parameter | Type | Location | Required | Description |
|---|---|---|---|---|
| `file` | File | Form | Yes | `.xlsx` or `.csv` file (max 10 MB, max 10,000 rows) |
| `dry_run` | boolean | Query | No | Default `false`. If `true`, runs full validation and reports counts but rolls back all DB changes |

#### File-Level Validations (synchronous, before job creation)

| Check | Rule | Error |
|---|---|---|
| File name | Must have `.xlsx` or `.csv` extension | 400 |
| File size | Must be ≤ 10 MB (10,485,760 bytes) | 400 |
| File content | Must not be empty | 400 |
| MIME type | Must be a recognized spreadsheet or text MIME | 400 |
| File integrity | Must be parseable by pandas (not corrupted/password-protected) | 400 |

#### Column Rules

| Column | Required | Type | Notes |
|---|---|---|---|
| `barcode` | **YES** | string | Primary key. Excel trailing `.0` is stripped automatically |
| `item_code` | No | string | SAP item code |
| `item_name` | No | string | Product display name |
| `sap_product_id` | No | string | SAP product identifier |
| `brand_name` | No | string | Auto-created if not found in DB |
| `category_name` | No | string | Auto-created if not found in DB |
| `subcategory_name` | No | string | Auto-created if not found in DB |
| `description` | No | string | Product description |
| `image_url` | No | string | Full URL to product image |
| `skin_type` | No | string | e.g. `dry`, `oily`, `sensitive`, `combination`, `normal` |
| `concerns` | No | string | Pipe or comma-separated: `acne\|dryness` or `acne,dryness` |
| `tags` | No | string | Comma-separated. Aliases `Tag_EN`, `Tag_MSA`, `Tag_IRQ` also accepted |
| `price` | No | number | SAP will overwrite on next sync. Allowed for initial seed only |
| `available_qty` | No | integer | SAP will overwrite on next sync |
| `price_tier` | No | enum | `Budget`, `Mid`, `Premium`, `Luxury` (case-insensitive) |
| `brand_family` | No | string | e.g. `Italian Niche`, `French Designer` (max 100 chars) |
| `product_status` | No | enum | `active`, `inactive`, `draft` (default `active`) |
| `is_best_selling` | No | boolean | Accepts: `1/0`, `true/false`, `yes/no` (case-insensitive) |
| `is_new_arrival` | No | boolean | Same boolean rules |
| `is_recommended` | No | boolean | Same boolean rules |
| `is_cod_recommended` | No | boolean | Same boolean rules |
| `recommendation_priority` | No | integer | 0–9999. Lower value = higher rank |
| `recommendation_score_override` | No | decimal | 0–999. Manual AI score override |
| `best_selling_scope` | No | string | `global`, `category`, `brand`, `subcategory` |
| `sales_rank` | No | integer | Legacy sales rank |
| `bundle_group` | No | — | **Deprecated — silently ignored** |
| `bundle_discount_percent` | No | — | **Deprecated — silently ignored** |

#### Success Response (202 Accepted)

```json
{
  "success": true,
  "job_id": "d4f3a1b2-9c81-4e7f-b890-abc123def456",
  "message": "Upload queued. Poll GET /products/uploads/{job_id} for progress.",
  "dry_run": false
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 400 | File validation failed (size, extension, MIME, corruption) |
| 500 | Failed to create upload job record in DB |

#### cURL Example

```bash
curl -X POST http://localhost:8000/products/upload \
  -F "file=@products.xlsx" \
  -F "dry_run=false"
```

```bash
# Dry run mode
curl -X POST "http://localhost:8000/products/upload?dry_run=true" \
  -F "file=@products.xlsx"
```

#### Postman Setup

- Method: `POST`
- URL: `{{base_url}}/products/upload`
- Body: `form-data`
  - Key: `file`, Type: File, Value: select your `.xlsx` or `.csv`
  - Key: `dry_run` (optional), Type: Text, Value: `false`

---

### 3.2 GET /products/uploads/{job_id}

**Purpose:** Poll the status and progress of a specific upload job.

**HTTP Method:** `GET`  
**URL:** `/products/uploads/{job_id}`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `job_id` | string (UUID) | Yes | The `job_id` returned by `POST /products/upload` |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "job_id": "d4f3a1b2-9c81-4e7f-b890-abc123def456",
    "filename": "products.xlsx",
    "status": "processing",
    "dry_run": false,
    "total_rows": 5000,
    "processed_rows": 1500,
    "progress_pct": 30,
    "created_count": 800,
    "updated_count": 650,
    "skipped_count": 50,
    "error_count": 5,
    "error_details": [
      {
        "row": 14,
        "barcode": "BC001234",
        "error": "barcode: String should have at least 1 character"
      }
    ],
    "error_message": null,
    "started_at": "2026-05-30T10:00:00.000000",
    "completed_at": null,
    "execution_seconds": null,
    "created_at": "2026-05-30T09:59:58.000000"
  }
}
```

#### Response Field Descriptions

| Field | Type | Description |
|---|---|---|
| `job_id` | string | UUID4 unique identifier |
| `filename` | string | Original uploaded filename |
| `status` | string | `queued` → `processing` → `completed` \| `failed` |
| `dry_run` | boolean | Whether this was a dry-run job |
| `total_rows` | integer | Total rows in the file (set after parsing) |
| `processed_rows` | integer | Rows processed so far (updated per 500-row batch) |
| `progress_pct` | integer | `round(processed_rows / total_rows × 100)` |
| `created_count` | integer | New products inserted |
| `updated_count` | integer | Existing products updated |
| `skipped_count` | integer | Rows skipped (validation failure or DB error) |
| `error_count` | integer | Total error rows |
| `error_details` | array | Up to 100 structured row errors (row number + barcode + message) |
| `error_message` | string\|null | Top-level failure reason if the entire job failed |
| `started_at` | ISO datetime\|null | When background processing began |
| `completed_at` | ISO datetime\|null | When processing finished |
| `execution_seconds` | float\|null | Total processing duration in seconds |
| `created_at` | ISO datetime | When the job record was created |

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 404 | Job ID not found |

#### cURL Example

```bash
curl http://localhost:8000/products/uploads/d4f3a1b2-9c81-4e7f-b890-abc123def456
```

---

### 3.3 GET /products/uploads

**Purpose:** Paginated list of all past upload jobs (newest first). Used by operations dashboards and audit trails.

**HTTP Method:** `GET`  
**URL:** `/products/uploads`

#### Query Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `page` | integer | No | `1` | Page number (min: 1) |
| `limit` | integer | No | `20` | Items per page (min: 1, max: 100) |
| `status` | string | No | None | Filter by status: `queued`, `processing`, `completed`, `failed` |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "jobs": [
      {
        "job_id": "d4f3a1b2-...",
        "filename": "products.xlsx",
        "status": "completed",
        "dry_run": false,
        "total_rows": 5000,
        "processed_rows": 5000,
        "progress_pct": 100,
        "created_count": 1200,
        "updated_count": 3750,
        "skipped_count": 50,
        "error_count": 5,
        "error_details": [...],
        "error_message": null,
        "started_at": "2026-05-30T10:00:01.000000",
        "completed_at": "2026-05-30T10:03:45.000000",
        "execution_seconds": 224.35,
        "created_at": "2026-05-30T10:00:00.000000"
      }
    ],
    "pagination": {
      "total": 47,
      "page": 1,
      "limit": 20,
      "total_pages": 3,
      "has_next": true,
      "has_prev": false
    }
  }
}
```

#### cURL Example

```bash
# All jobs
curl "http://localhost:8000/products/uploads?page=1&limit=20"

# Only failed jobs
curl "http://localhost:8000/products/uploads?status=failed"
```

---

### 3.4 GET /products/upload-template

**Purpose:** Return the column schema so the frontend can generate a downloadable template spreadsheet.

**HTTP Method:** `GET`  
**URL:** `/products/upload-template`

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "required_columns": ["barcode"],
    "all_supported_columns": [
      "barcode", "item_code", "item_name", "sap_product_id",
      "brand_name", "category_name", "subcategory_name",
      "description", "image_url", "skin_type", "concerns", "tags",
      "price", "available_qty", "price_tier", "brand_family",
      "product_status", "is_best_selling", "is_new_arrival",
      "is_recommended", "is_cod_recommended", "recommendation_priority",
      "recommendation_score_override", "best_selling_scope", "sales_rank"
    ],
    "accepted_file_types": [".xlsx", ".csv"],
    "limits": {
      "max_file_size_mb": 10,
      "max_rows": 10000
    },
    "notes": [
      "barcode is the only required column.",
      "First sheet will be used for Excel files.",
      "concerns and tags should be pipe- or comma-separated.",
      "Existing products are updated when barcode matches.",
      "Booleans accept: 1/0, true/false, yes/no (case-insensitive).",
      "bundle_group and bundle_discount_percent are no longer supported — use the /bundles/* API to manage bundles."
    ]
  }
}
```

#### cURL Example

```bash
curl http://localhost:8000/products/upload-template
```

---

## 4. Product Management APIs

### 4.1 DELETE /products/{barcode}

**Purpose:** Soft-delete a product. Sets `deleted_at` timestamp; the product row is retained in the database. The product is excluded from all public-facing queries immediately. Restore with `POST /products/{barcode}/restore`.

**HTTP Method:** `DELETE`  
**URL:** `/products/{barcode}`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | Product barcode (primary key) |

#### Success Response (200 OK)

```json
{
  "success": true,
  "message": "Product soft-deleted. Use POST /products/{barcode}/restore to undo.",
  "barcode": "BC001234",
  "item_name": "Dior Sauvage EDP 100ml",
  "deleted_at": "2026-05-30T11:30:00.000000"
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 404 | Product not found or already deleted |

#### cURL Example

```bash
curl -X DELETE http://localhost:8000/products/BC001234
```

---

### 4.2 POST /products/{barcode}/restore

**Purpose:** Restore a soft-deleted product by clearing its `deleted_at` timestamp.

**HTTP Method:** `POST`  
**URL:** `/products/{barcode}/restore`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | Product barcode |

#### Success Response (200 OK)

```json
{
  "success": true,
  "message": "Product restored successfully.",
  "data": {
    "barcode": "BC001234",
    "item_name": "Dior Sauvage EDP 100ml",
    "product_status": "active"
  }
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 404 | Product row does not exist |
| 422 | Product is not currently in a deleted state |
| 500 | Database commit failure |

#### cURL Example

```bash
curl -X POST http://localhost:8000/products/BC001234/restore
```

---

### 4.3 PATCH /products/{barcode}/status

**Purpose:** Transition a product to a new status with full audit logging.

**HTTP Method:** `PATCH`  
**URL:** `/products/{barcode}/status`

#### Allowed Status Transitions

| From | To | Allowed |
|---|---|---|
| `draft` | `active` | Yes |
| `draft` | `inactive` | Yes |
| `active` | `draft` | Yes |
| `active` | `inactive` | Yes |
| `inactive` | `active` | Yes |
| `inactive` | `draft` | **No** — re-activate first |

#### Request Body

```json
{
  "status": "active",
  "changed_by": "admin_user_id",
  "reason": "seasonal launch Q3 2026"
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `status` | string | **Yes** | `active` \| `inactive` \| `draft` | Target status |
| `changed_by` | string | No | max 255 chars | User ID or system label stored in audit log. Defaults to `"system"` |
| `reason` | string | No | — | Free-text note recorded in audit log |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "barcode": "BC001234",
    "old_status": "inactive",
    "new_status": "active",
    "changed_by": "admin_user_id",
    "changed_at": "2026-05-30T12:00:00.000000"
  }
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 404 | Product not found |
| 422 | Invalid transition (e.g. inactive → draft) or invalid status value |
| 500 | Database commit failure |

#### cURL Example

```bash
curl -X PATCH http://localhost:8000/products/BC001234/status \
  -H "Content-Type: application/json" \
  -d '{
    "status": "active",
    "changed_by": "admin_user_id",
    "reason": "seasonal launch"
  }'
```

---

### 4.4 POST /products/bulk/status

**Purpose:** Transition up to 500 products to a new status in one request. Valid and invalid products are processed independently — an error for one product does not block the others.

**HTTP Method:** `POST`  
**URL:** `/products/bulk/status`

#### Request Body

```json
{
  "barcodes": ["BC001", "BC002", "BC003"],
  "status": "inactive",
  "changed_by": "admin_user_id",
  "reason": "End of season — Q2 2026"
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `barcodes` | array of string | **Yes** | 1–500 items | Product barcodes to update |
| `status` | string | **Yes** | `active` \| `inactive` \| `draft` | Target status for all barcodes |
| `changed_by` | string | No | max 255 chars | User ID or label for audit log |
| `reason` | string | No | — | Free-text note for audit log |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "updated": ["BC001", "BC003"],
    "skipped": ["BC002"],
    "errors": [
      {
        "barcode": "BC999",
        "error": "Product not found"
      }
    ]
  }
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 422 | Request body validation failed (e.g. more than 500 barcodes, invalid status) |
| 500 | Database commit failure |

#### cURL Example

```bash
curl -X POST http://localhost:8000/products/bulk/status \
  -H "Content-Type: application/json" \
  -d '{
    "barcodes": ["BC001", "BC002", "BC003"],
    "status": "inactive",
    "changed_by": "admin_user",
    "reason": "End of season Q2 2026"
  }'
```

---

### 4.5 PATCH /products/{barcode}/flags

**Purpose:** Update editorial and recommendation flags for a single product. All fields are optional — only supplied fields are changed. SAP sync will never overwrite these fields.

**HTTP Method:** `PATCH`  
**URL:** `/products/{barcode}/flags`

#### Request Body

```json
{
  "is_recommended": true,
  "price_tier": "Premium",
  "recommendation_priority": 5,
  "is_new_arrival": true
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `is_recommended` | boolean | No | — | Include in AI recommendation pool |
| `is_new_arrival` | boolean | No | — | Mark as new arrival (boosts freshness score) |
| `is_best_selling` | boolean | No | — | Mark as best seller (boosts popularity score) |
| `is_cod_recommended` | boolean | No | — | Flag for Cash-on-Delivery recommendation |
| `recommendation_priority` | integer | No | 0–9999 | Lower number = higher rank in recommendation lists |
| `recommendation_score_override` | float | No | 0–999 | Manual override for AI scoring weight |
| `price_tier` | string | No | `Budget` \| `Mid` \| `Premium` \| `Luxury` | Product price classification |
| `brand_family` | string | No | max 100 chars | e.g. `Italian Niche`, `French Designer` |
| `best_selling_scope` | string | No | `global` \| `category` \| `brand` \| `subcategory` | Scope of best-selling classification |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "barcode": "BC001234",
    "item_name": "Dior Sauvage EDP 100ml",
    "updated_flags": {
      "is_recommended": true,
      "price_tier": "Premium",
      "recommendation_priority": 5
    }
  }
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 404 | Product not found |
| 422 | Invalid field value (e.g. priority > 9999, invalid price_tier) |
| 500 | Database commit failure |

#### cURL Example

```bash
curl -X PATCH http://localhost:8000/products/BC001234/flags \
  -H "Content-Type: application/json" \
  -d '{
    "is_recommended": true,
    "price_tier": "Premium",
    "recommendation_priority": 5
  }'
```

---

### 4.6 POST /products/bulk/flags

**Purpose:** Apply the same editorial flags to up to 500 products at once. Products not found are listed in `not_found` but do not fail the request.

**HTTP Method:** `POST`  
**URL:** `/products/bulk/flags`

#### Request Body

```json
{
  "barcodes": ["BC001", "BC002", "BC003"],
  "flags": {
    "is_new_arrival": true,
    "price_tier": "Premium"
  }
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `barcodes` | array of string | **Yes** | 1–500 items | Target product barcodes |
| `flags` | FlagsUpdateRequest | **Yes** | See § 4.5 | Flag fields to apply. Only supplied fields are changed |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "updated": ["BC001", "BC002"],
    "not_found": ["BC003"]
  }
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 422 | Request validation failed |
| 500 | Database commit failure |

#### cURL Example

```bash
curl -X POST http://localhost:8000/products/bulk/flags \
  -H "Content-Type: application/json" \
  -d '{
    "barcodes": ["BC001", "BC002"],
    "flags": {
      "is_new_arrival": true,
      "price_tier": "Premium"
    }
  }'
```

---

## 5. Health & Monitoring APIs

### 5.1 GET /health

**Purpose:** Liveness probe. Returns 200 immediately if the process is alive. Does **not** check external dependencies. Used by load balancers and container orchestrators.

**HTTP Method:** `GET`  
**URL:** `/health`

#### Success Response (200 OK)

```json
{
  "status": "healthy",
  "timestamp": "2026-05-30T10:00:00.000000+00:00"
}
```

#### cURL Example

```bash
curl http://localhost:8000/health
```

---

### 5.2 GET /ready

**Purpose:** Readiness probe. Checks that all external dependencies are reachable before the instance receives traffic. Returns 200 when ready, 503 when not.

**HTTP Method:** `GET`  
**URL:** `/ready`

#### Checks Performed

| Check | Condition | Status |
|---|---|---|
| `database` | PostgreSQL responds to `SELECT 1` | `ok` \| `error` |
| `sap_sync` | Last SAP sync within 25 hours | `success` \| `warning` \| `error` |

#### Success Response (200 OK)

```json
{
  "ready": true,
  "timestamp": "2026-05-30T10:00:00.000000+00:00",
  "checks": {
    "database": {
      "status": "ok"
    },
    "sap_sync": {
      "status": "success",
      "last_sync": "2026-05-30T06:00:05.000000",
      "age_hours": 4.0
    }
  }
}
```

#### Not Ready Response (503 Service Unavailable)

```json
{
  "ready": false,
  "timestamp": "2026-05-30T10:00:00.000000+00:00",
  "checks": {
    "database": {
      "status": "error",
      "detail": "could not connect to server"
    },
    "sap_sync": {
      "status": "warning",
      "detail": "No sync run recorded yet"
    }
  }
}
```

#### cURL Example

```bash
curl http://localhost:8000/ready
```

---

## 6. AI Recommendation APIs

> **Graceful Degradation:** All vector-based endpoints fall back gracefully when a product has no embedding yet, rather than returning an error. This ensures the system works from day one with 0% embedding coverage.

> **Product Filter (all endpoints):** Only products meeting all of the following are returned: `product_status = 'active'`, `available_qty > 5`, `price > 0`, `deleted_at IS NULL`.

---

### 6.1 POST /ai/recommend

**Purpose:** Universal intent-aware recommendation endpoint. The primary entry point for chatbot and API clients. Detects the user's intent, routes to the optimal retrieval strategy, applies hybrid scoring, and returns personalised results.

**HTTP Method:** `POST`  
**URL:** `/ai/recommend`

#### Intent Detection & Routing

The endpoint uses GPT-4o-mini to classify the query into one of:

| Intent | Trigger | Strategy |
|---|---|---|
| `similar_product` | "Something like Dior Sauvage" | Embed reference name → cosine search |
| `skin_concern` | "My skin is dry and sensitive" | Embed skin text + metadata filtering |
| `category_search` | "Show me luxury face creams" | Category + price_tier vector search |
| `brand_search` | "I want something from Chanel" | Brand name vector search |
| `price_search` | "Budget skincare under 15,000 IQD" | Price-filtered vector search |
| `general` | Any other query | Hybrid semantic search |

#### Request Body

```json
{
  "query": "I need a perfume similar to Dior Sauvage",
  "user_id": "usr_123",
  "session_id": "sess_abc",
  "locale": "en",
  "limit": 8,
  "category_id": null,
  "price_tier": "Premium",
  "cart_barcodes": ["BC001"],
  "viewed_barcodes": ["BC002", "BC003"],
  "include_scores": false
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `query` | string | **Yes** | 1–500 chars | User's natural-language query (English or Arabic) |
| `user_id` | string | No | — | User ID for personalisation and preference learning |
| `session_id` | string | No | — | Session token for multi-turn context |
| `locale` | string | No | `en` \| `ar` | Response language hint. Default `en` |
| `limit` | integer | No | 1–50 | Max products to return. Default `10` |
| `category_id` | integer | No | — | Pre-filter by category FK |
| `price_tier` | string | No | `Budget` \| `Mid` \| `Premium` \| `Luxury` | Price tier filter |
| `cart_barcodes` | array of string | No | — | Products currently in cart — excluded from results |
| `viewed_barcodes` | array of string | No | — | Products already seen this session — deprioritised to end of list |
| `include_scores` | boolean | No | — | Include internal `_scores` breakdown in each product. Default `false` |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 42,
    "returned": 8,
    "strategy": "ai_similar_product",
    "intent": "similar_product",
    "reference_name": "Dior Sauvage",
    "detected_skin_type": null,
    "detected_concerns": null,
    "detected_category": "fragrance",
    "detected_price_tier": "Premium",
    "products": [
      {
        "id": "BC001234",
        "barcode": "BC001234",
        "name": "Bleu de Chanel EDP 100ml",
        "brand": "Chanel",
        "category": "Fragrance",
        "subcategory": "Eau de Parfum",
        "description": "Fresh aromatic fragrance with notes of grapefruit and sandalwood",
        "image_url": "https://cdn.example.com/products/BC001234.jpg",
        "price": "85,000 IQD",
        "raw_price": 64.9,
        "available_qty": 23,
        "skin_type": null,
        "concerns": [],
        "tags": ["bestseller", "fresh"],
        "price_tier": "Premium",
        "brand_family": "French Designer",
        "is_best_selling": true,
        "is_new_arrival": false,
        "order_link": "https://shop.example.com/order/BC001234"
      }
    ]
  }
}
```

#### Response with `include_scores=true`

Each product object gains a `_scores` field:

```json
"_scores": {
  "final": 0.7842,
  "semantic": 0.891,
  "editorial": 0.654,
  "popularity": 0.700,
  "stock": 0.230,
  "freshness": 0.000,
  "reason": "hybrid_semantic"
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 422 | Invalid `price_tier` value, `query` too long, or `limit` out of range |
| 500 | OpenAI API failure or database error |

#### cURL Example

```bash
curl -X POST http://localhost:8000/ai/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "query": "I need a perfume similar to Dior Sauvage",
    "user_id": "usr_123",
    "limit": 8,
    "price_tier": "Premium",
    "include_scores": true
  }'
```

---

### 6.2 POST /ai/search

**Purpose:** Pure semantic search. Embeds the query and finds nearest catalog matches. No intent detection overhead. Use when the caller already knows the query is a search (not a conversational message).

**HTTP Method:** `POST`  
**URL:** `/ai/search`

#### Request Body

```json
{
  "query": "moisturizer for dry skin",
  "limit": 10,
  "category_id": 3,
  "price_tier": "Mid",
  "min_price": 10.0,
  "max_price": 50.0,
  "skin_type": "dry",
  "concerns": ["dryness", "sensitivity"],
  "include_scores": false
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `query` | string | **Yes** | 1–500 chars | Search query text |
| `limit` | integer | No | 1–50 | Max results. Default `10` |
| `category_id` | integer | No | — | Filter by category FK |
| `price_tier` | string | No | `Budget` \| `Mid` \| `Premium` \| `Luxury` | Price tier filter |
| `min_price` | float | No | — | Minimum price (USD, before IQD conversion) |
| `max_price` | float | No | — | Maximum price (USD, before IQD conversion) |
| `skin_type` | string | No | — | Filter by skin type (partial match, case-insensitive) |
| `concerns` | array of string | No | — | Filter to products matching ANY concern |
| `include_scores` | boolean | No | — | Include `_scores` in response. Default `false` |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 18,
    "returned": 10,
    "strategy": "semantic_search",
    "intent": null,
    "products": [ ... ]
  }
}
```

#### cURL Example

```bash
curl -X POST http://localhost:8000/ai/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "moisturizer for dry sensitive skin",
    "skin_type": "dry",
    "concerns": ["dryness", "sensitivity"],
    "price_tier": "Mid",
    "limit": 10
  }'
```

---

### 6.3 GET /ai/similar/{barcode}

**Purpose:** Returns products most similar to the given barcode's embedding vector. Falls back to featured products if the source product has no embedding yet.

**HTTP Method:** `GET`  
**URL:** `/ai/similar/{barcode}`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | Source product barcode |

#### Query Parameters

| Parameter | Type | Required | Default | Validation | Description |
|---|---|---|---|---|---|
| `limit` | integer | No | `10` | 1–50 | Max similar products to return |
| `same_category` | boolean | No | `false` | — | If true, restrict to the same product category |
| `include_scores` | boolean | No | `false` | — | Include `_scores` in response |

#### Success Response (200 OK) — With Embedding

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 25,
    "returned": 10,
    "strategy": "product_similarity",
    "intent": null,
    "source_barcode": "BC001234",
    "same_category": false,
    "products": [ ... ]
  }
}
```

#### Fallback Response (200 OK) — No Embedding

```json
{
  "success": true,
  "fallback": true,
  "data": {
    "found": true,
    "strategy": "similar_fallback_featured",
    "source_barcode": "BC001234",
    "products": [ ... ]
  }
}
```

#### cURL Example

```bash
curl "http://localhost:8000/ai/similar/BC001234?limit=10&same_category=false"
```

---

### 6.4 GET /ai/cross-sell/{barcode}

**Purpose:** Returns complementary products from a different category for cross-sell placement. "Customers who bought this also bought..."

**HTTP Method:** `GET`  
**URL:** `/ai/cross-sell/{barcode}`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | Source product barcode |

#### Query Parameters

| Parameter | Type | Required | Default | Validation | Description |
|---|---|---|---|---|---|
| `limit` | integer | No | `8` | 1–30 | Max cross-sell products |
| `include_scores` | boolean | No | `false` | — | Include scoring breakdown |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 15,
    "returned": 8,
    "strategy": "cross_sell",
    "intent": null,
    "source_barcode": "BC001234",
    "products": [ ... ]
  }
}
```

#### cURL Example

```bash
curl "http://localhost:8000/ai/cross-sell/BC001234?limit=8"
```

---

### 6.5 GET /ai/upsell/{barcode}

**Purpose:** Returns higher-tier alternatives in the same category for upsell placement. "Upgrade to a premium version..." Only returns products priced at least 10% more than the source.

**HTTP Method:** `GET`  
**URL:** `/ai/upsell/{barcode}`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | Source product barcode |

#### Query Parameters

| Parameter | Type | Required | Default | Validation | Description |
|---|---|---|---|---|---|
| `limit` | integer | No | `6` | 1–20 | Max upsell products |
| `include_scores` | boolean | No | `false` | — | Include scoring breakdown |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 8,
    "returned": 6,
    "strategy": "upsell",
    "intent": null,
    "source_barcode": "BC001234",
    "source_price": 45.0,
    "products": [ ... ]
  }
}
```

#### Fallback When No Higher-Priced Alternatives

```json
{
  "success": true,
  "data": {
    "found": false,
    "strategy": "upsell",
    "reason": "Source product has no valid price",
    "products": []
  }
}
```

#### cURL Example

```bash
curl "http://localhost:8000/ai/upsell/BC001234?limit=6"
```

---

### 6.6 POST /ai/skincare

**Purpose:** Returns skincare product recommendations tailored to skin type and specific concerns. Uses semantic search enriched with skin-typed query text.

**HTTP Method:** `POST`  
**URL:** `/ai/skincare`

#### Request Body

```json
{
  "skin_type": "dry",
  "concerns": ["dryness", "sensitivity", "aging"],
  "price_tier": "Mid",
  "category_id": null,
  "limit": 10,
  "include_scores": false
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `skin_type` | string | No | — | `dry` \| `oily` \| `sensitive` \| `combination` \| `normal` |
| `concerns` | array of string | No | — | Any of: `acne`, `dryness`, `oiliness`, `sensitivity`, `aging`, `hyperpigmentation`, `dark_circles`, `pores`, `redness`, `eczema` |
| `price_tier` | string | No | `Budget` \| `Mid` \| `Premium` \| `Luxury` | Price tier filter |
| `category_id` | integer | No | — | Further restrict to a category FK |
| `limit` | integer | No | 1–50 | Max results. Default `10` |
| `include_scores` | boolean | No | — | Include `_scores`. Default `false` |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 22,
    "returned": 10,
    "strategy": "skincare_ai",
    "intent": null,
    "skin_type": "dry",
    "concerns": ["dryness", "sensitivity", "aging"],
    "products": [ ... ]
  }
}
```

#### cURL Example

```bash
curl -X POST http://localhost:8000/ai/skincare \
  -H "Content-Type: application/json" \
  -d '{
    "skin_type": "dry",
    "concerns": ["dryness", "sensitivity"],
    "price_tier": "Mid",
    "limit": 10
  }'
```

---

### 6.7 POST /ai/fragrance

**Purpose:** Find perfumes similar to a named reference fragrance. The reference name is automatically enriched with fragrance domain vocabulary before embedding. Results are sorted: fragrance-category products first, then others.

**HTTP Method:** `POST`  
**URL:** `/ai/fragrance`

#### Request Body

```json
{
  "reference_name": "Dior Sauvage",
  "price_tier": "Premium",
  "limit": 8,
  "include_scores": false
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `reference_name` | string | **Yes** | 1–200 chars | Reference perfume name (brand + product name) |
| `price_tier` | string | No | `Budget` \| `Mid` \| `Premium` \| `Luxury` | Filter by price tier |
| `limit` | integer | No | 1–50 | Max results. Default `10` |
| `include_scores` | boolean | No | — | Include `_scores`. Default `false` |

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 31,
    "returned": 8,
    "strategy": "fragrance_similarity",
    "intent": null,
    "reference_name": "Dior Sauvage",
    "products": [ ... ]
  }
}
```

#### cURL Example

```bash
curl -X POST http://localhost:8000/ai/fragrance \
  -H "Content-Type: application/json" \
  -d '{
    "reference_name": "Dior Sauvage",
    "price_tier": "Premium",
    "limit": 8
  }'
```

---

### 6.8 GET /ai/personalised

**Purpose:** Personalised recommendations that blend the query embedding with the user's stored preference embedding (built from past clicks, purchases, and accepted recommendations). Falls back to standard semantic search if no preference profile exists.

**HTTP Method:** `GET`  
**URL:** `/ai/personalised`

#### Query Parameters

| Parameter | Type | Required | Default | Validation | Description |
|---|---|---|---|---|---|
| `user_id` | string | **Yes** | — | — | User ID (must have events logged for personalisation) |
| `q` | string | No | `"beauty products"` | — | Search context query |
| `category_id` | integer | No | `null` | — | Filter by category FK |
| `limit` | integer | No | `10` | 1–50 | Max results |
| `include_scores` | boolean | No | `false` | — | Include `_scores` |

#### Blending Formula

```
blended_vector = 0.7 × query_embedding + 0.3 × user_preference_embedding
blended_vector = normalize(blended_vector)  # unit length
```

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "found": true,
    "total_found": 18,
    "returned": 10,
    "strategy": "personalised",
    "intent": null,
    "products": [ ... ]
  }
}
```

#### cURL Example

```bash
curl "http://localhost:8000/ai/personalised?user_id=usr_123&q=skincare&limit=10"
```

---

## 7. AI Embedding APIs

### 7.1 GET /ai/embeddings/status

**Purpose:** Returns current embedding coverage statistics. Use this to monitor pipeline health and decide when to activate `RECOMMENDATION_SCORER=hybrid_ai`.

**HTTP Method:** `GET`  
**URL:** `/ai/embeddings/status`

#### Success Response (200 OK)

```json
{
  "success": true,
  "data": {
    "total_products": 12500,
    "embedded_products": 8750,
    "pending_products": 3750,
    "coverage_pct": 70.0,
    "embed_model": "text-embedding-3-small",
    "embed_dimensions": 1536
  }
}
```

#### Response Field Descriptions

| Field | Type | Description |
|---|---|---|
| `total_products` | integer | All active (non-deleted) products |
| `embedded_products` | integer | Products with `embedding IS NOT NULL` |
| `pending_products` | integer | Products still awaiting embedding |
| `coverage_pct` | float | `embedded / total × 100`, one decimal place |
| `embed_model` | string | OpenAI model used (`text-embedding-3-small`) |
| `embed_dimensions` | integer | Vector dimensions (1536) |

#### cURL Example

```bash
curl http://localhost:8000/ai/embeddings/status
```

---

### 7.2 POST /ai/embeddings/trigger

**Purpose:** Trigger the product embedding background job on-demand. Returns immediately; the job runs asynchronously. Check `/ai/embeddings/status` for progress.

**HTTP Method:** `POST`  
**URL:** `/ai/embeddings/trigger`

#### Request Body (all optional)

```json
{
  "limit": 0,
  "force_all": false,
  "batch_size": 50
}
```

#### Request Body Schema

| Field | Type | Required | Default | Validation | Description |
|---|---|---|---|---|---|
| `limit` | integer | No | `0` | ≥0 | Max products to embed. `0` = all pending |
| `force_all` | boolean | No | `false` | — | If `true`, re-embed the entire catalog (ignores stale check). Use after prompt changes |
| `batch_size` | integer | No | `50` | 1–200 | Products per OpenAI API call |

#### Success Response (200 OK)

```json
{
  "success": true,
  "message": "Embedding job queued. Processing all products. Poll GET /ai/embeddings/status for progress.",
  "force_all": false,
  "batch_size": 50
}
```

#### cURL Examples

```bash
# Embed all pending products
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -H "Content-Type: application/json" \
  -d '{}'

# Re-embed entire catalog (after prompt change)
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -H "Content-Type: application/json" \
  -d '{"force_all": true, "batch_size": 100}'

# Embed first 500 products only (test run)
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -H "Content-Type: application/json" \
  -d '{"limit": 500}'
```

---

### 7.3 PATCH /products/{barcode}/embedding/refresh

**Purpose:** Immediately re-generate the embedding for a single product. Use after editing a product's name, description, or tags. Runs **synchronously** — returns the new `embedding_updated_at` timestamp.

**HTTP Method:** `PATCH`  
**URL:** `/products/{barcode}/embedding/refresh`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | Product barcode |

#### No Request Body Required

#### Success Response (200 OK)

```json
{
  "success": true,
  "barcode": "BC001234",
  "embedding_updated_at": "2026-05-30T12:30:00.000000",
  "ai_score": 0.723145,
  "embedding_text_len": 342
}
```

#### Response Field Descriptions

| Field | Type | Description |
|---|---|---|
| `barcode` | string | Product barcode |
| `embedding_updated_at` | ISO datetime | Timestamp of the new embedding |
| `ai_score` | float | Newly computed base AI score (0.0–1.0) |
| `embedding_text_len` | integer | Character length of text sent to OpenAI |

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 404 | Product not found or soft-deleted |
| 422 | Product has no text fields to embed |
| 500 | OpenAI returned empty vector or database commit failure |

#### cURL Example

```bash
curl -X PATCH http://localhost:8000/products/BC001234/embedding/refresh
```

---

## 8. Behavioral Tracking APIs

### 8.1 POST /events/product/{barcode}

**Purpose:** Log a behavioral event (view, click, purchase, recommendation accepted/rejected). Events feed into:
1. User preference embedding updates (async)
2. Future collaborative-filtering models
3. AI score recalculation pipeline

**HTTP Method:** `POST`  
**URL:** `/events/product/{barcode}`

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `barcode` | string | Yes | The product barcode the event is associated with |

#### Request Body

```json
{
  "user_id": "usr_123",
  "session_id": "sess_abc",
  "event_type": "purchase",
  "source": "chatbot",
  "position": 2,
  "metadata": {
    "query": "perfume similar to Dior Sauvage",
    "rec_type": "similar_product",
    "ab_group": "B"
  }
}
```

#### Request Body Schema

| Field | Type | Required | Validation | Description |
|---|---|---|---|---|
| `user_id` | string | No | — | User ID. Required for preference profile update |
| `session_id` | string | No | — | Session token for multi-turn tracking |
| `event_type` | string | **Yes** | See enum below | Type of interaction |
| `source` | string | No | — | Origin: `chatbot`, `api`, `frontend`, `recommendation` |
| `position` | integer | No | ≥0 | Rank position in the recommendation list (0-indexed) |
| `metadata` | object | No | — | Free-form JSON (query text, rec type, A/B group, etc.) |

#### Event Type Enum

| Value | Weight | Triggers Profile Update |
|---|---|---|
| `view` | 0.3 | No |
| `click` | 0.7 | Yes |
| `purchase` | 1.0 | Yes |
| `recommendation_accepted` | 0.9 | Yes |
| `recommendation_rejected` | — | No |

#### Success Response (200 OK)

```json
{
  "success": true,
  "message": "Event logged.",
  "event_type": "purchase",
  "barcode": "BC001234"
}
```

#### Error Responses

| HTTP Code | Condition |
|---|---|
| 422 | Invalid `event_type` value |
| 500 | Database commit failure |

#### cURL Example

```bash
curl -X POST http://localhost:8000/events/product/BC001234 \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "usr_123",
    "event_type": "purchase",
    "source": "chatbot",
    "position": 2
  }'
```

---

## 9. Data Models

### 9.1 UploadJob

Table: `upload_jobs`

| Column | Type | Description |
|---|---|---|
| `id` | VARCHAR(36) PK | UUID4 string |
| `filename` | VARCHAR(500) | Original uploaded filename |
| `status` | VARCHAR(20) | `queued` \| `processing` \| `completed` \| `failed` |
| `dry_run` | BOOLEAN | Whether this was a simulation run |
| `total_rows` | INTEGER | Total rows in the file |
| `processed_rows` | INTEGER | Rows processed so far |
| `created_count` | INTEGER | New products inserted |
| `updated_count` | INTEGER | Existing products updated |
| `skipped_count` | INTEGER | Rows skipped |
| `error_count` | INTEGER | Total error rows |
| `error_details` | JSONB | Up to 100 row-level errors: `[{row, barcode, error}]` |
| `error_message` | TEXT | Top-level failure reason |
| `started_at` | DATETIME | Background processing start time |
| `completed_at` | DATETIME | Completion time |
| `execution_seconds` | NUMERIC(10,2) | Processing duration |
| `created_at` | DATETIME | Job creation time |

**Indexes:** `idx_upload_jobs_status`, `idx_upload_jobs_created_at`

---

### 9.2 ProductEvent

Table: `product_events`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | VARCHAR(255) | User identifier (nullable) |
| `session_id` | VARCHAR(255) | Session token (nullable) |
| `barcode` | VARCHAR(100) | Product barcode |
| `event_type` | VARCHAR(50) | `view`, `click`, `purchase`, `recommendation_accepted`, `recommendation_rejected` |
| `source` | VARCHAR(50) | `chatbot`, `recommendation_api`, `frontend`, `search` |
| `position` | INTEGER | Rank in recommendation list (nullable) |
| `metadata` | JSONB | Free-form event context |
| `created_at` | DATETIME | Event timestamp |

**Indexes:** `idx_product_events_user_barcode` (user_id, barcode, created_at), `idx_product_events_barcode_type` (barcode, event_type, created_at)

---

### 9.3 UserPreferenceProfile

Table: `user_preference_profiles`

| Column | Type | Description |
|---|---|---|
| `user_id` | VARCHAR(255) PK | One row per user |
| `embedding` | VECTOR(1536) | Mean-pooled preference embedding (unit normalized) |
| `preferred_categories` | JSONB | Frequency map: `{"Skincare": 12, "Fragrance": 4}` |
| `preferred_brands` | JSONB | Frequency map: `{"Dior": 3, "Chanel": 2}` |
| `preferred_price_tiers` | JSONB | Frequency map: `{"Premium": 8, "Mid": 2}` |
| `preferred_skin_types` | JSONB | Frequency map: `{"dry": 5, "sensitive": 3}` |
| `total_events` | INTEGER | Total positive events recorded |
| `last_updated` | DATETIME | Last profile update timestamp |

**Cold start:** On first positive event, `embedding = product_embedding` directly.  
**Update strategy:** Exponential moving average — `new = (1-α) × old + α × product_embedding`, then unit-normalize.

---

### 9.4 Product Embedding Fields

On the `products` table:

| Column | Type | Description |
|---|---|---|
| `embedding` | VECTOR(1536) | OpenAI `text-embedding-3-small` vector (NULL until pipeline runs) |
| `embedding_text` | TEXT | The prose block sent to OpenAI for generation |
| `embedding_updated_at` | DATETIME | When the embedding was last generated |
| `ai_score` | NUMERIC(8,6) | Cached composite score (0.0–1.0). Updated by embedding pipeline |

**Embedding text format:**
```
Product: {item_name}
Brand: {brand_name}
Category: {category_name}
Sub-category: {subcategory_name}
Description: {description}
Skin type: {skin_type}
Price tier: {price_tier}
Brand family: {brand_family}
Concerns: {concern1}, {concern2}
Tags: {tag1}, {tag2}
```
Max 8,000 characters (~2,000 tokens), well under the 8,192-token model limit.

---

### 9.5 ProductStatusLog (Audit)

Table: `product_status_log`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `barcode` | VARCHAR(100) | Product barcode |
| `from_status` | VARCHAR(20) | Previous status (NULL for first assignment) |
| `to_status` | VARCHAR(20) | New status |
| `changed_by` | VARCHAR(255) | User ID or `"system"` |
| `reason` | TEXT | Optional free-text note |
| `changed_at` | DATETIME | Timestamp of the change |

This table is **append-only**. Never UPDATE or DELETE rows.

---

## 10. Recommendation Scoring

### 10.1 Hybrid Scoring Formula

```
final_score = W_SEM   × semantic_similarity
            + W_ED    × editorial_score
            + W_POP   × popularity_score
            + W_STK   × availability_score
            + W_FRESH × freshness_score
```

**Default Weights** (all environment-configurable):

| Component | Env Var | Default | Description |
|---|---|---|---|
| Semantic | `SCORE_W_SEMANTIC` | `0.40` | 1 − cosine_distance from pgvector |
| Editorial | `SCORE_W_EDITORIAL` | `0.22` | Priority + override + flag bonuses |
| Popularity | `SCORE_W_POPULARITY` | `0.15` | Best-selling flag + sales rank |
| Stock | `SCORE_W_STOCK` | `0.13` | Available quantity / 100, capped at 1.0 |
| Freshness | `SCORE_W_FRESHNESS` | `0.10` | New arrival recency bonus |

### 10.2 Component Details

**Semantic Score** (0.0–1.0)
```
semantic_similarity = 1.0 - cosine_distance
# cosine_distance = p.embedding <=> query_vector (pgvector <=> operator)
```

**Editorial Score** (0.0–1.0)
```python
s_priority = 1.0 - (min(recommendation_priority, 9999) / 9999)
s_override  = min(recommendation_score_override, 999) / 999
flag_bonus  = 0.0
if is_best_selling:    flag_bonus += 0.50
if is_new_arrival:     flag_bonus += 0.30
flag_bonus = min(flag_bonus, 1.0)

editorial_score = 0.4 × s_priority + 0.3 × s_override + 0.3 × flag_bonus
```

**Popularity Score** (0.0–1.0)
```python
s_popularity = 0.7 if is_best_selling else 0.0
if sales_rank <= 10:   s_popularity = max(s_popularity, 1.0)
elif sales_rank <= 50: s_popularity = max(s_popularity, 0.7)
elif sales_rank <= 200:s_popularity = max(s_popularity, 0.4)
else:                  s_popularity = max(s_popularity, 0.1)
```

**Availability Score** (0.0–1.0)
```python
stock_score = min(1.0, max(0.0, available_qty) / 100)
# Full score at 100+ units; zero at 0 units
```

**Freshness Score** (0.0 or 0.8)
```python
freshness_score = 0.8 if is_new_arrival else 0.0
```

### 10.3 Base AI Score (Stored in `products.ai_score`)

Computed by the embedding pipeline after each product is embedded:

```python
editorial  = 0.4 × priority_score + 0.3 × override_score + 0.3 × flag_bonus
  # flag_bonus: is_recommended=0.4, is_best_selling=0.35, is_new_arrival=0.15, is_cod_recommended=0.10

stock      = min(1.0, available_qty / 100)
freshness  = 0.3 if is_new_arrival else 0.0

base_score = 0.5 × editorial + 0.3 × stock + 0.2 × freshness
# Range: 0.0 – 1.0
```

### 10.4 HybridAIScorer (when `RECOMMENDATION_SCORER=hybrid_ai`)

After vector retrieval, re-ranks the product list:

```python
final = SCORE_ALPHA × ai_score + (1 - SCORE_ALPHA) × editorial_score
# SCORE_ALPHA default = 0.55 (env: SCORE_ALPHA)
```

**Activation threshold:** Recommended when embedding coverage ≥ 50% (check `GET /ai/embeddings/status`).

---

## 11. Error Reference

### 11.1 Standard Error Envelope

All errors return:
```json
{
  "success": false,
  "error": "Human-readable error message."
}
```

### 11.2 HTTP Status Code Reference

| HTTP Code | Name | When It Occurs |
|---|---|---|
| 200 | OK | Request succeeded |
| 202 | Accepted | Upload job queued (POST /products/upload) |
| 400 | Bad Request | File validation failure (size, extension, MIME, corruption) |
| 404 | Not Found | Product, upload job, or resource does not exist |
| 422 | Unprocessable Entity | Request body validation failed (invalid field values, missing required fields) |
| 500 | Internal Server Error | Database commit failure, OpenAI API error, or unexpected exception |
| 503 | Service Unavailable | Readiness check failed (DB unreachable) |

### 11.3 Domain Error Codes

| Error Message Pattern | Type | Cause |
|---|---|---|
| `"Product '{barcode}' not found."` | NotFoundError (404) | Barcode does not exist in the database |
| `"Upload job '{job_id}' not found."` | NotFoundError (404) | Invalid or expired job ID |
| `"File is too large ({N} MB)..."` | HTTPException (400) | File exceeds 10 MB |
| `"Unsupported file type '{ext}'..."` | HTTPException (400) | Not `.xlsx` or `.csv` |
| `"Uploaded file is empty."` | HTTPException (400) | Zero-byte file |
| `"File could not be parsed..."` | HTTPException (400) | Corrupted or password-protected file |
| `"Missing required columns: barcode"` | Job failed | Upload file missing the `barcode` column |
| `"File has {N} rows which exceeds the limit of 10000..."` | Job failed | Too many rows — split the file |
| `"Invalid price_tier '{value}'..."` | AppValidationError (422) | price_tier not in allowed enum |
| `"Product '{barcode}' has no text fields to embed."` | AppValidationError (422) | Cannot generate embedding — no content fields |
| `"Embedding generation failed: ..."` | ServiceError (500) | OpenAI API error during single-product refresh |
| `"AI recommendation failed. Please try again."` | ServiceError (500) | OpenAI or DB error in recommendation pipeline |
| `"Update failed due to a database error."` | ServiceError (500) | DB commit error during product update |
| `"Restore failed due to a database error."` | ServiceError (500) | DB commit error during product restore |
| `"Failed to log product event."` | ServiceError (500) | DB commit error during event logging |
| `"An unexpected error occurred. Please try again later."` | 500 | Catch-all for unhandled exceptions (full trace logged server-side) |

---

## 12. Deployment Guide

### 12.1 Required Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | **Yes** | — | PostgreSQL connection string. Must include `?sslmode=require` for production |
| `OPENAI_API_KEY` | **Yes** | `""` | OpenAI API key. Embedding and recommendation pipelines will silently skip without it |
| `RECOMMENDATION_SCORER` | No | `rule_based` | `rule_based` \| `personalization` \| `hybrid_ai` |
| `SCORE_ALPHA` | No | `0.55` | HybridAIScorer AI weight (0.0–1.0). Only applies when `RECOMMENDATION_SCORER=hybrid_ai` |
| `SCORE_W_SEMANTIC` | No | `0.40` | Hybrid search weight for semantic similarity |
| `SCORE_W_EDITORIAL` | No | `0.22` | Hybrid search weight for editorial score |
| `SCORE_W_POPULARITY` | No | `0.15` | Hybrid search weight for popularity |
| `SCORE_W_STOCK` | No | `0.13` | Hybrid search weight for stock availability |
| `SCORE_W_FRESHNESS` | No | `0.10` | Hybrid search weight for freshness |
| `LEADS_API_URL` | No | — | External URL for lead capture webhook |
| `ORDER_BASE_URL` | No | `""` | Base URL for product order links (`{ORDER_BASE_URL}/{barcode}`) |

### 12.2 pgvector Requirements

pgvector is required for all AI-powered endpoints. The `products` and `user_preference_profiles` tables use `VECTOR(1536)` columns.

**Installation:**
```bash
# PostgreSQL extension (run as superuser)
CREATE EXTENSION IF NOT EXISTS vector;

# Python package
pip install pgvector
```

**HNSW Index (required for production performance):**
```sql
-- Create HNSW index on product embeddings for fast cosine search
CREATE INDEX idx_products_embedding_hnsw
ON products USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

Without pgvector, the app starts with a warning and RAG/vector features are unavailable, but all non-AI endpoints continue to function.

### 12.3 OpenAI Requirements

- **Model used:** `text-embedding-3-small` (embeddings), `gpt-4o-mini` (intent detection), `gpt-4o` (chatbot)
- **Rate limits:** OpenAI tier-1 allows 3,000 RPM and 1M TPM on embeddings. The pipeline sleeps 0.5s between batches of 50 at ~200 tokens/product
- **Estimated cost:** ~$0.10 for a full catalog re-embed of 100,000 products

### 12.4 Migration Requirements

Alembic migrations must be run before starting the app:

```bash
# Apply all migrations
alembic upgrade head
```

**Critical migrations:**
- `20260530_001_production_improvements.py` — Adds `product_status`, `price_tier`, `brand_family`, embedding columns, pgvector extension
- `20260530_002_ai_recommendation_engine.py` — Creates `product_events`, `user_preference_profiles`, `upload_jobs` tables

```bash
# Verify current migration state
alembic current

# View migration history
alembic history --verbose
```

### 12.5 Background Scheduler

The app auto-starts an APScheduler (Iraq/Baghdad timezone) on startup:

| Job | Schedule | Description |
|---|---|---|
| SAP sync | 06:00 and 18:00 IQ | Syncs price and stock from SAP |
| Embedding pipeline | 03:00 IQ daily | Processes un-embedded products (`embedding IS NULL`) |

---

## 13. Operational Guide

### 13.1 How to Trigger Embeddings

**Option A: API (recommended for ad-hoc runs)**
```bash
# Embed all pending products
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -H "Content-Type: application/json" \
  -d '{}'

# Force re-embed entire catalog (use after changing embedding text format)
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -H "Content-Type: application/json" \
  -d '{"force_all": true}'

# Test run: embed first 100 products only
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -H "Content-Type: application/json" \
  -d '{"limit": 100, "batch_size": 50}'
```

**Option B: Single product refresh (after editing product content)**
```bash
curl -X PATCH http://localhost:8000/products/BC001234/embedding/refresh
```

**Option C: Automatic** — The scheduler runs daily at 03:00 IQ time.

### 13.2 How to Monitor Embedding Progress

```bash
# Check coverage stats
curl http://localhost:8000/ai/embeddings/status

# Expected response when complete:
# { "coverage_pct": 100.0, "pending_products": 0 }
```

**Coverage thresholds:**

| Coverage | Recommendation |
|---|---|
| 0–10% | Use `RECOMMENDATION_SCORER=rule_based` (default). AI endpoints will return empty with no errors |
| 10–50% | AI search works. Keep `rule_based` scorer |
| ≥50% | Activate `RECOMMENDATION_SCORER=hybrid_ai` for best results |
| 100% | Optimal. Set `RECOMMENDATION_SCORER=hybrid_ai` |

### 13.3 How to Verify Recommendation Engine Health

```bash
# 1. Check if vector search works
curl -X POST http://localhost:8000/ai/search \
  -H "Content-Type: application/json" \
  -d '{"query": "perfume", "limit": 3}'

# 2. Check embedding coverage
curl http://localhost:8000/ai/embeddings/status

# 3. Test intent-aware recommendation
curl -X POST http://localhost:8000/ai/recommend \
  -H "Content-Type: application/json" \
  -d '{"query": "moisturizer for dry skin", "include_scores": true}'

# 4. Confirm similar products works
curl "http://localhost:8000/ai/similar/{known_barcode}?limit=5&include_scores=true"

# 5. Check overall service health
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

**Healthy system indicators:**
- `GET /health` → `{"status": "healthy"}`
- `GET /ready` → `{"ready": true}`
- `GET /ai/embeddings/status` → `coverage_pct > 0`
- `POST /ai/search` → `{"found": true}` with products

### 13.4 Troubleshooting Guide

#### Problem: POST /ai/recommend returns `{"found": false}`

**Causes & Fixes:**
1. No embeddings generated yet → run `POST /ai/embeddings/trigger`
2. `OPENAI_API_KEY` not set → check environment variables
3. All products filtered out (out of stock / inactive) → check product statuses
4. Query embedding failed → check OpenAI API status and key validity

#### Problem: Embedding job does nothing / "All products are already embedded"

**Cause:** All active products already have embeddings.  
**Fix:** If you updated product content, use `force_all=true`:
```bash
curl -X POST http://localhost:8000/ai/embeddings/trigger \
  -d '{"force_all": true}'
```

#### Problem: Upload job stuck in `processing` status

**Causes:**
1. Background task thread crashed → check application logs for `upload_job_failed`
2. Database connectivity issue → check `GET /ready`
3. File had 10,000 rows and is still processing — each 500-row batch is one DB commit

**Fix:** Query the job status — if `error_message` is set, the job failed:
```bash
curl http://localhost:8000/products/uploads/{job_id}
```

#### Problem: Large upload file has many `skipped` rows

**Cause:** Rows failed Pydantic validation.  
**Fix:** Check `error_details` in the job response. Common causes:
- Empty or whitespace-only `barcode` column
- Invalid boolean values in flag columns (use `1/0`, `true/false`, `yes/no`)
- Invalid `price_tier` (must be exactly `Budget`, `Mid`, `Premium`, or `Luxury`)

#### Problem: PATCH /products/{barcode}/embedding/refresh returns 422

**Cause:** Product has no text fields (item_name, description, brand, category all empty).  
**Fix:** Update the product with at least one content field via `PUT /products/{barcode}` first.

#### Problem: GET /ready returns 503

**Check `checks.database`:**
- `"status": "error"` → PostgreSQL is unreachable. Check DB connection string and network.

**Check `checks.sap_sync`:**
- `"status": "warning"` with `age_hours > 25` → SAP sync has not run. Trigger manually:
  ```bash
  curl -X POST http://localhost:8000/sap/sync-now
  ```
- `"detail": "No sync run recorded yet"` → First deployment; expected on day 1.

#### Problem: AI recommendations always return same products

**Cause:** `RECOMMENDATION_SCORER=rule_based` with identical `recommendation_priority`.  
**Fix:**
1. Set varied `recommendation_priority` values via `PATCH /products/{barcode}/flags`
2. Generate embeddings and switch to `hybrid_ai` scorer for dynamic ranking

#### Problem: Personalised recommendations identical to regular recommendations

**Cause:** User has no preference profile (no `click`/`purchase`/`recommendation_accepted` events).  
**Fix:** Log events via `POST /events/product/{barcode}`. Profile is built on the first positive event.

---

*This documentation was generated directly from the implemented source code. All endpoint behaviors, schemas, validation rules, and response formats reflect the actual running implementation as of 2026-05-30.*
