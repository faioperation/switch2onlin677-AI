"""
T1-T8 production tests for PUT /products/{barcode}
Confirmed pre-existing barcodes in PostgreSQL DB (current state):
  BC_SRC   = '100101'        item_code='012-0001'  name='مناشف + صداري'
  BC_CONFL = '3605521159991' item_code='RESTORED_3605521159991'  name='Restored Product'
Guaranteed-new barcode prefix:
  BC_NEW   = 'KILORENEW<timestamp>'
"""
import json, os, time, sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r"D:\Kishor\Projects\switch2onlin677\AI")

import logging
logging.disable(logging.WARNING)   # silence PK_CHANGE + uvicorn/propagate noise

os.environ["DATABASE_URL"] = "postgresql://postgres:admin123@localhost:5432/simple_test_db"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from main import app

client = TestClient(app)
pg = create_engine("postgresql://postgres:admin123@localhost:5432/simple_test_db")

# barcode used as rename target — guaranteed fresh datetime-based ID
KILORENEW = f"KILORENEW{int(time.time())}"

BC_SRC   = "3605521159991"      # confirmed existing, alphanumeric (RESTORED_3605521159991)
BC_CONFL = "100102"             # another existing product with different item_code for conflict tests
BC_NEW   = KILORENEW            # datetime-stamped, guaranteed fresh
BC_NIX   = "PRODUCT99ZZ9"       # never exists
BC_BADF  = "bad space!"         # triggers format validation

results = {}          # label -> bool
titles  = {}          # label -> human-readable title


def upd(label, title, path, body, expect, check):
    """Run PUT, record result, print short status line."""
    r      = client.put(path, json=body)
    b      = r.json()
    ok     = r.status_code == expect and check(b)
    results[label] = ok
    titles[label]  = title
    mark          = "PASS" if ok else "FAIL"
    print(f"  {title[:50]:50s} {r.status_code} {mark}")
    if not ok:
        print(f"    body={json.dumps(b, ensure_ascii=False)[:200]}")
    return ok


def gget(label, title, path, expect, check):
    """Run GET, record result, print short status line."""
    r    = client.get(path)
    b    = r.json()
    ok   = r.status_code == expect and check(b)
    results[label] = ok
    titles[label]  = title
    mark          = "PASS" if ok else "FAIL"
    print(f"  {title[:50]:50s} {r.status_code} {mark}")
    if not ok:
        print(f"    body={json.dumps(b, ensure_ascii=False)[:200]}")
    return ok


def psql(query, params=None):
    """Execute a raw SQL read."""
    with pg.connect() as c:
        return c.execute(text(query), params or {}).fetchall()


# ── Pre-flight: confirm BC_SRC exists ──────────────────────────────────────────
pre = psql("SELECT barcode, item_code, item_name FROM products WHERE barcode=:bc", {"bc": BC_SRC})
assert pre, f"BC_SRC={BC_SRC!r} not in DB — seed first"
print(f"Pre-flight: {BC_SRC!r} exists, item_code={pre[0][1]!r}, item_name={pre[0][2][:40]}")

BC_OLD = BC_SRC   # will be overwritten by T2 after rename

results["PRE"] = True
titles["PRE"]  = f"Pre-flight: {BC_SRC} confirmed in DB"

# ───────────────────────────────────────────────────────────────────────────────
print()
print("═" * 65)
print("  Category 1 — happy paths & successful updates")
print("═" * 65)

# ─── T1: Update item_name only → 200 ──────────────────────────────────────────
upd("T1", "T1: item_name only -> 200",
    f"/products/{BC_SRC}",
    {"item_name": "T1-UpdatedName"},
    200,
    lambda b: b.get("success") is True
              and b.get("data", {}).get("item_name") == "T1-UpdatedName"
              and "updated_at" in b.get("data", {}))

# Restore item_name (so confusion table stays stable for later tests)
client.put(f"/products/{BC_SRC}", json={"item_name": pre[0][2][:80]})

# ─── T2: Barcode rename to NEW value → 200+PK warning ─────────────────────────
upd("T2", "T2: barcode rename to new -> 200+warning",
    f"/products/{BC_SRC}",
    {"barcode": BC_NEW, "item_name": "T2-Renamed"},
    200,
    lambda b: b.get("success") is True
              and b.get("data", {}).get("barcode") == BC_NEW
              and b.get("data", {}).get("item_name") == "T2-Renamed"
              and "warnings" in b)

BC_OLD = BC_SRC   # remember old PK before it disappears

# ─── T2A: GET by new barcode → 200 ─────────────────────────────────────────────
gget("T2A", "T2A: GET /products/{new_barcode} -> 200",
    f"/products/{BC_NEW}",
    200,
    lambda b: b.get("success") is True
              and b.get("data", {}).get("barcode") == BC_NEW
              and b.get("data", {}).get("item_name") == "T2-Renamed")

# ─── T2B: GET by old barcode → 404 (old PK must be gone) ──────────────────────
gget("T2B", "T2B: GET /products/{old_barcode} -> 404",
    f"/products/{BC_OLD}",
    404,
    lambda b: "not found" in str(b.get("detail", b.get("error", ""))).lower())

# ─── T8: productsearchindex renamed + rebuilt in PostgreSQL ───────────────────
row = psql(
    "SELECT product_id, item_code, item_name, brand_name, "
    "category_name, subcategory_name, search_text "
    "FROM productsearchindex WHERE product_id = :pid",
    {"pid": BC_NEW},
)
t8_present = len(row) == 1
t8_correct = (t8_present
              and row[0][2] == "T2-Renamed"
              and row[0][6] is not None   # search_text must be non-null
              and "renamed" in row[0][6].lower())
results["T8"] = t8_present and t8_correct
titles["T8"]  = "T8: productsearchindex PK renamed + search_text rebuilt"
print(f"  {'T8: productsearchindex PK renamed + search_text rebuilt':50s} {'PASS' if results['T8'] else 'FAIL'}")
if not t8_present:
    print(f"    productsearchindex[{BC_NEW}] NOT FOUND")

# ─── T5: Update barcode+name together → 200 ───────────────────────────────────
upd("T5", "T5: barcode+name together -> 200",
    f"/products/{BC_NEW}",
    {"barcode": BC_NEW, "item_name": "T5-BothTogether"},
    200,
    lambda b: b.get("success") is True
              and b.get("data", {}).get("barcode") == BC_NEW
              and b.get("data", {}).get("item_name") == "T5-BothTogether")

# ───────────────────────────────────────────────────────────────────────────────
print()
print("═" * 65)
print("  Category 2 — conflicts & validation errors")
print("═" * 65)

# ─── T3: Barcode to existing value → 409 ───────────────────────────────────────
upd("T3", "T3: barcode to existing value -> 409",
    f"/products/{BC_NEW}",
    {"barcode": BC_CONFL},
    409,
    lambda b: "already exists" in b.get("error", ""))

# ─── T4: item_code duplicate → 409 ─────────────────────────────────────────────
upd("T4", "T4: item_code duplicate -> 409",
    f"/products/{BC_NEW}",
    {"item_code": "RESTORED_3605521159991"},
    409,
    lambda b: "already used" in b.get("error", ""))

# ─── T6: Bad barcode format → 422 ───────────────────────────────────────────────
upd("T6", "T6: bad barcode format -> 422",
    f"/products/{BC_NEW}",
    {"barcode": BC_BADF},
    422,
    lambda b: "barcode" in b.get("error", "").lower())

# ─── T7: Non-existent product → 404 ───────────────────────────────────────────
upd("T7", "T7: non-existent product -> 404",
    f"/products/{BC_NIX}",
    {"item_name": "whatever"},
    404,
    lambda b: "not found" in str(b.get("detail", b.get("error", ""))).lower())

# ───────────────────────────────────────────────────────────────────────────────
print()
print("═" * 65)
print("  Category 3 — range / FK / enum validation")
print("═" * 65)

upd("V1", "V1: price < 0               -> 422",
    f"/products/{BC_NEW}", {"price": -1.0}, 422,
    lambda b: "price" in b.get("error", "").lower())

upd("V2", "V2: available_qty < 0       -> 422",
    f"/products/{BC_NEW}", {"available_qty": -1}, 422,
    lambda b: "available_qty" in b.get("error", "").lower())

upd("V3", "V3: sales_rank 0            -> 422",
    f"/products/{BC_NEW}", {"sales_rank": 0}, 422,
    lambda b: "sales_rank" in b.get("error", "").lower())

upd("V4", "V4: is_best_selling = 2      -> 422",
    f"/products/{BC_NEW}", {"is_best_selling": 2}, 422,
    lambda b: "is_best_selling" in b.get("error", "").lower())

upd("V5", "V5: best_selling_scope bad -> 422",
    f"/products/{BC_NEW}", {"best_selling_scope": "bad"}, 422,
    lambda b: "best_selling_scope" in b.get("error", "").lower())

upd("V6", "V6: brand_id nonexistent   -> 422",
    f"/products/{BC_NEW}", {"brand_id": 9999}, 422,
    lambda b: "does not exist" in b.get("error", ""))

upd("V7", "V7: category_id nonexistent -> 422",
    f"/products/{BC_NEW}", {"category_id": 9999}, 422,
    lambda b: "does not exist" in b.get("error", ""))

upd("V8", "V8: subcategory_id nonexistent -> 422",
    f"/products/{BC_NEW}", {"subcategory_id": 9999}, 422,
    lambda b: "does not exist" in b.get("error", ""))

# ───────────────────────────────────────────────────────────────────────────────
print()
print("═" * 65)
print("  Category 4 — protected-field & edge-case guards")
print("═" * 65)

upd("P1", "P1: last_synced_sap blocked -> 422",
    f"/products/{BC_NEW}", {"last_synced_sap": "2024-01-01"}, 422,
    lambda b: "Cannot modify protected" in b.get("error", ""))

upd("P2", "P2: created_at blocked     -> 422",
    f"/products/{BC_NEW}", {"created_at": "2024-01-01"}, 422,
    lambda b: "Cannot modify protected" in b.get("error", ""))

upd("E1", "E1: empty body             -> 200 (no-op)",
    f"/products/{BC_NEW}", {}, 200,
    lambda b: b.get("success") is True)

upd("E2", "E2: tags + concerns arrays  -> 200",
    f"/products/{BC_NEW}", {"tags": ["bestseller"], "concerns": ["acne"]}, 200,
    lambda b: b.get("success") is True)

upd("E3", "E3: barcode empty string    -> 422",
    f"/products/{BC_NEW}", {"barcode": ""}, 422,
    lambda b: "barcode" in b.get("error", "").lower())

upd("E4", "E4: barcode too short (3)   -> 422",
    f"/products/{BC_NEW}", {"barcode": "ABC"}, 422,
    lambda b: "barcode" in b.get("error", "").lower())

upd("E5", "E5: barcode 33 chars         -> 422",
    f"/products/{BC_NEW}", {"barcode": "A" * 33}, 422,
    lambda b: "barcode" in b.get("error", "").lower())

# ───────────────────────────────────────────────────────────────────────────────
print()
print("═" * 65)
print("  SUMMARY")
print("═" * 65)
passed  = sum(results.values())
total   = len(results)
failing = [k for k, v in results.items() if not v]
print(f"\n  RESULTS: {passed}/{total} passed\n")
for k in sorted(results):
    mark = "✅ PASS" if results[k] else "❌ FAIL"
    print(f"    {mark}  {k}: {titles[k]}")

if failing:
    print(f"\n  ❌ Failures: {', '.join(failing)}")
else:
    print("\n  ✅ All checks passed")
