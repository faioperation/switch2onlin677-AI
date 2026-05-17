"""
tests/test_categories.py
========================
Unit and integration tests for:
  GET /api/v1/categories
  GET /api/v1/categories/{id}

Run with:
    pytest tests/test_categories.py -v
"""

import pytest
from models import Brand, Category, Product, Subcategory


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_category(db, name, is_active=1, name_ar=None):
    cat = Category(name=name, name_ar=name_ar, is_active=is_active)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def _make_subcategory(db, name, category_id, is_active=1):
    sub = Subcategory(name=name, category_id=category_id, is_active=is_active)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _make_product(db, barcode, item_code, item_name, category_id=None, brand_id=None, subcategory_id=None):
    p = Product(
        barcode=barcode,
        item_code=item_code,
        item_name=item_name,
        category_id=category_id,
        brand_id=brand_id,
        subcategory_id=subcategory_id,
    )
    db.add(p)
    db.commit()
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/v1/categories  — LIST
# ═══════════════════════════════════════════════════════════════════════════════

class TestListCategories:

    def test_empty_database_returns_success(self, client):
        """Empty DB → success=true, empty list, pagination zeros."""
        resp = client.get("/api/v1/categories")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["categories"] == []
        assert body["data"]["pagination"]["total"] == 0
        assert body["data"]["pagination"]["total_pages"] == 0

    def test_returns_all_categories(self, client, db):
        """Three categories in DB → all three returned."""
        _make_category(db, "Skincare")
        _make_category(db, "Perfume")
        _make_category(db, "Makeup")

        resp = client.get("/api/v1/categories")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["pagination"]["total"] == 3
        assert len(data["categories"]) == 3

    def test_pagination_page_2(self, client, db):
        """25 categories, page=2&limit=10 → 10 items, correct pagination meta."""
        for i in range(25):
            _make_category(db, f"Cat {i:02d}")

        resp = client.get("/api/v1/categories?page=2&limit=10")
        assert resp.status_code == 200
        data = resp.json()["data"]
        pg = data["pagination"]
        assert pg["total"] == 25
        assert pg["page"] == 2
        assert pg["limit"] == 10
        assert pg["total_pages"] == 3
        assert pg["has_prev"] is True
        assert pg["has_next"] is True
        assert len(data["categories"]) == 10

    def test_pagination_last_page(self, client, db):
        """25 categories, page=3&limit=10 → 5 items, has_next=False."""
        for i in range(25):
            _make_category(db, f"Cat {i:02d}")

        resp = client.get("/api/v1/categories?page=3&limit=10")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["categories"]) == 5
        assert data["pagination"]["has_next"] is False

    def test_search_partial_match(self, client, db):
        """search=skin filters by case-insensitive partial match."""
        _make_category(db, "Skincare")
        _make_category(db, "Haircare")
        _make_category(db, "SkinToner")

        resp = client.get("/api/v1/categories?search=skin")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["data"]["categories"]]
        assert "Skincare" in names
        assert "SkinToner" in names
        assert "Haircare" not in names

    def test_search_case_insensitive(self, client, db):
        _make_category(db, "SKINCARE")
        resp = client.get("/api/v1/categories?search=skincare")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["categories"]) == 1

    def test_is_active_filter_true(self, client, db):
        """is_active=true returns only active categories."""
        _make_category(db, "Active Cat", is_active=1)
        _make_category(db, "Inactive Cat", is_active=0)

        resp = client.get("/api/v1/categories?is_active=true")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["data"]["categories"]]
        assert "Active Cat" in names
        assert "Inactive Cat" not in names

    def test_is_active_filter_false(self, client, db):
        """is_active=false returns only inactive categories."""
        _make_category(db, "Active Cat", is_active=1)
        _make_category(db, "Inactive Cat", is_active=0)

        resp = client.get("/api/v1/categories?is_active=false")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["data"]["categories"]]
        assert "Inactive Cat" in names
        assert "Active Cat" not in names

    def test_include_counts_returns_product_and_subcategory_counts(self, client, db):
        """include_counts=true attaches correct product_count and subcategory_count."""
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Toner", cat.id)
        _make_subcategory(db, "Serum", cat.id)
        _make_product(db, "B001", "IC001", "Product A", category_id=cat.id)
        _make_product(db, "B002", "IC002", "Product B", category_id=cat.id)
        _make_product(db, "B003", "IC003", "Product C", category_id=cat.id)

        resp = client.get("/api/v1/categories?include_counts=true")
        assert resp.status_code == 200
        cats = resp.json()["data"]["categories"]
        assert len(cats) == 1
        assert cats[0]["product_count"] == 3
        assert cats[0]["subcategory_count"] == 2

    def test_include_counts_false_omits_count_fields(self, client, db):
        """include_counts=false (default) → no product_count or subcategory_count keys."""
        _make_category(db, "Skincare")
        resp = client.get("/api/v1/categories")
        cat = resp.json()["data"]["categories"][0]
        assert "product_count" not in cat
        assert "subcategory_count" not in cat

    def test_sort_by_name_asc(self, client, db):
        """sort_by=name&sort_order=asc → alphabetical order."""
        _make_category(db, "Zzz")
        _make_category(db, "Aaa")
        _make_category(db, "Mmm")

        resp = client.get("/api/v1/categories?sort_by=name&sort_order=asc")
        names = [c["name"] for c in resp.json()["data"]["categories"]]
        assert names == sorted(names)

    def test_sort_by_name_desc(self, client, db):
        _make_category(db, "Zzz")
        _make_category(db, "Aaa")

        resp = client.get("/api/v1/categories?sort_by=name&sort_order=desc")
        names = [c["name"] for c in resp.json()["data"]["categories"]]
        assert names[0] == "Zzz"
        assert names[-1] == "Aaa"

    def test_sort_by_product_count(self, client, db):
        """sort_by=product_count&sort_order=desc → most products first."""
        cat_a = _make_category(db, "A")
        cat_b = _make_category(db, "B")
        # cat_a gets 1 product, cat_b gets 3
        _make_product(db, "BA1", "IA1", "P1", category_id=cat_a.id)
        _make_product(db, "BB1", "IB1", "P2", category_id=cat_b.id)
        _make_product(db, "BB2", "IB2", "P3", category_id=cat_b.id)
        _make_product(db, "BB3", "IB3", "P4", category_id=cat_b.id)

        resp = client.get(
            "/api/v1/categories?sort_by=product_count&sort_order=desc&include_counts=true"
        )
        assert resp.status_code == 200
        cats = resp.json()["data"]["categories"]
        assert cats[0]["name"] == "B"
        assert cats[0]["product_count"] == 3
        assert cats[1]["product_count"] == 1

    # ── Validation failures ────────────────────────────────────────────────────

    def test_limit_above_100_returns_400(self, client):
        resp = client.get("/api/v1/categories?limit=101")
        assert resp.status_code == 400
        # FastAPI wraps HTTPException.detail in {"detail": ...}
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error"]["code"] == "VALIDATION_ERROR"

    def test_limit_zero_returns_400(self, client):
        resp = client.get("/api/v1/categories?limit=0")
        assert resp.status_code == 400

    def test_invalid_sort_by_returns_400(self, client):
        resp = client.get("/api/v1/categories?sort_by=invalid_column")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_sort_order_returns_400(self, client):
        resp = client.get("/api/v1/categories?sort_order=sideways")
        assert resp.status_code == 400

    def test_limit_100_is_allowed(self, client):
        """Boundary: limit=100 must not raise an error."""
        resp = client.get("/api/v1/categories?limit=100")
        assert resp.status_code == 200

    def test_tenant_isolation_not_yet_implemented(self, client, db):
        """
        Placeholder: tenant isolation is not implemented because the `categories`
        table has no `business_id` column and no JWT auth middleware exists.

        TODO(auth): When business_id is added to the schema and JWT is wired up,
        replace this test with one that:
          1. Creates category A under business 1.
          2. Creates category B under business 2.
          3. Authenticates as business 1 and asserts category B is NOT returned.
          4. Authenticates as business 2 and asserts category A is NOT returned.
        """
        pytest.skip("Tenant isolation requires business_id column + JWT auth middleware")


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/v1/categories/{id}  — DETAIL
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetCategory:

    def test_returns_category_detail(self, client, db):
        """Happy path: correct fields are present and accurate."""
        cat = _make_category(db, "Skincare", name_ar="عناية بالبشرة")
        resp = client.get(f"/api/v1/categories/{cat.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert data["id"] == cat.id
        assert data["name"] == "Skincare"
        assert data["name_ar"] == "عناية بالبشرة"
        assert data["is_active"] is True

    def test_includes_nested_subcategories(self, client, db):
        """Detail response includes subcategories list with id, name, slug."""
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Toner", cat.id)
        _make_subcategory(db, "Serum", cat.id)

        resp = client.get(f"/api/v1/categories/{cat.id}")
        data = resp.json()["data"]
        assert len(data["subcategories"]) == 2
        names = [s["name"] for s in data["subcategories"]]
        assert "Toner" in names
        assert "Serum" in names
        # slug is null until slug column is added to the schema
        assert all(s["slug"] is None for s in data["subcategories"])

    def test_subcategories_sorted_alphabetically(self, client, db):
        """Nested subcategories are returned in ascending name order."""
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Zzz Sub", cat.id)
        _make_subcategory(db, "Aaa Sub", cat.id)

        resp = client.get(f"/api/v1/categories/{cat.id}")
        subs = resp.json()["data"]["subcategories"]
        names = [s["name"] for s in subs]
        assert names == sorted(names)

    def test_includes_correct_product_count(self, client, db):
        """product_count reflects the number of products in this category."""
        cat = _make_category(db, "Skincare")
        _make_product(db, "B001", "IC001", "P1", category_id=cat.id)
        _make_product(db, "B002", "IC002", "P2", category_id=cat.id)

        resp = client.get(f"/api/v1/categories/{cat.id}")
        assert resp.json()["data"]["product_count"] == 2

    def test_product_count_zero_for_empty_category(self, client, db):
        cat = _make_category(db, "Empty")
        resp = client.get(f"/api/v1/categories/{cat.id}")
        assert resp.json()["data"]["product_count"] == 0

    def test_products_from_other_categories_not_counted(self, client, db):
        """product_count must NOT include products belonging to other categories."""
        cat_a = _make_category(db, "A")
        cat_b = _make_category(db, "B")
        _make_product(db, "BA1", "IA1", "P1", category_id=cat_a.id)
        _make_product(db, "BB1", "IB1", "P2", category_id=cat_b.id)

        resp = client.get(f"/api/v1/categories/{cat_a.id}")
        assert resp.json()["data"]["product_count"] == 1

    def test_not_found_returns_404(self, client):
        resp = client.get("/api/v1/categories/99999")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error"]["code"] == "NOT_FOUND"

    def test_invalid_id_type_returns_422(self, client):
        """Non-integer ID → FastAPI returns 422 (Unprocessable Entity)."""
        resp = client.get("/api/v1/categories/not-an-integer")
        assert resp.status_code == 422
