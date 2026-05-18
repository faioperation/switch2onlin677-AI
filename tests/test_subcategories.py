"""
tests/test_subcategories.py
===========================
Unit and integration tests for:
  GET /api/v1/subcategories
  GET /api/v1/subcategories/{id}

Run with:
    pytest tests/test_subcategories.py -v
"""

import pytest
from models import Brand, Category, Product, Subcategory


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_category(db, name, is_active=1):
    c = Category(name=name, is_active=is_active)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_subcategory(db, name, category_id=None, is_active=1, name_ar=None):
    s = Subcategory(name=name, name_ar=name_ar, category_id=category_id, is_active=is_active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_product(db, barcode, item_code, item_name, subcategory_id=None, category_id=None, brand_id=None):
    p = Product(
        barcode=barcode,
        item_code=item_code,
        item_name=item_name,
        subcategory_id=subcategory_id,
        category_id=category_id,
        brand_id=brand_id,
    )
    db.add(p)
    db.commit()
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/v1/subcategories  — LIST
# ═══════════════════════════════════════════════════════════════════════════════

class TestListSubcategories:

    def test_empty_database_returns_success(self, client):
        resp = client.get("/api/v1/subcategories")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["subcategories"] == []
        assert body["data"]["pagination"]["total"] == 0

    def test_returns_all_subcategories(self, client, db):
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Toner", cat.id)
        _make_subcategory(db, "Serum", cat.id)
        _make_subcategory(db, "Moisturizer", cat.id)

        resp = client.get("/api/v1/subcategories")
        data = resp.json()["data"]
        assert data["pagination"]["total"] == 3
        assert len(data["subcategories"]) == 3

    def test_response_includes_nested_parent_category(self, client, db):
        """Each subcategory in the list includes its parent category."""
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Toner", cat.id)

        resp = client.get("/api/v1/subcategories")
        sub = resp.json()["data"]["subcategories"][0]
        assert "category" in sub
        assert sub["category"]["id"] == cat.id
        assert sub["category"]["name"] == "Skincare"

    def test_category_id_filter(self, client, db):
        """category_id filter returns only subcategories under that parent."""
        cat_skin = _make_category(db, "Skincare")
        cat_hair = _make_category(db, "Haircare")
        _make_subcategory(db, "Toner", cat_skin.id)
        _make_subcategory(db, "Serum", cat_skin.id)
        _make_subcategory(db, "Shampoo", cat_hair.id)

        resp = client.get(f"/api/v1/subcategories?category_id={cat_skin.id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["pagination"]["total"] == 2
        names = [s["name"] for s in data["subcategories"]]
        assert "Toner" in names
        assert "Serum" in names
        assert "Shampoo" not in names

    def test_category_id_filter_empty_when_no_match(self, client, db):
        cat = _make_category(db, "Skincare")
        # No subcategories in this category

        resp = client.get(f"/api/v1/subcategories?category_id={cat.id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["subcategories"] == []

    def test_pagination(self, client, db):
        """25 subcategories, page=2&limit=10 → correct pagination meta."""
        cat = _make_category(db, "Skincare")
        for i in range(25):
            _make_subcategory(db, f"Sub {i:02d}", cat.id)

        resp = client.get("/api/v1/subcategories?page=2&limit=10")
        assert resp.status_code == 200
        pg = resp.json()["data"]["pagination"]
        assert pg["total"] == 25
        assert pg["page"] == 2
        assert pg["total_pages"] == 3
        assert pg["has_prev"] is True
        assert pg["has_next"] is True
        assert len(resp.json()["data"]["subcategories"]) == 10

    def test_search_partial_match(self, client, db):
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Vitamin C Serum", cat.id)
        _make_subcategory(db, "Retinol Serum", cat.id)
        _make_subcategory(db, "Toner", cat.id)

        resp = client.get("/api/v1/subcategories?search=serum")
        names = [s["name"] for s in resp.json()["data"]["subcategories"]]
        assert "Vitamin C Serum" in names
        assert "Retinol Serum" in names
        assert "Toner" not in names

    def test_is_active_filter(self, client, db):
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Active Sub", cat.id, is_active=1)
        _make_subcategory(db, "Inactive Sub", cat.id, is_active=0)

        resp = client.get("/api/v1/subcategories?is_active=true")
        names = [s["name"] for s in resp.json()["data"]["subcategories"]]
        assert "Active Sub" in names
        assert "Inactive Sub" not in names

    def test_include_counts_attaches_product_count(self, client, db):
        cat = _make_category(db, "Skincare")
        sub = _make_subcategory(db, "Toner", cat.id)
        _make_product(db, "B001", "IC001", "P1", subcategory_id=sub.id)
        _make_product(db, "B002", "IC002", "P2", subcategory_id=sub.id)

        resp = client.get("/api/v1/subcategories?include_counts=true")
        assert resp.status_code == 200
        subs = resp.json()["data"]["subcategories"]
        assert subs[0]["product_count"] == 2

    def test_include_counts_false_omits_count_field(self, client, db):
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Toner", cat.id)

        resp = client.get("/api/v1/subcategories")
        sub = resp.json()["data"]["subcategories"][0]
        assert "product_count" not in sub

    def test_sort_by_name_asc(self, client, db):
        cat = _make_category(db, "Skincare")
        _make_subcategory(db, "Zzz", cat.id)
        _make_subcategory(db, "Aaa", cat.id)
        _make_subcategory(db, "Mmm", cat.id)

        resp = client.get("/api/v1/subcategories?sort_by=name&sort_order=asc")
        names = [s["name"] for s in resp.json()["data"]["subcategories"]]
        assert names == sorted(names)

    def test_sort_by_product_count_desc(self, client, db):
        cat = _make_category(db, "Skincare")
        sub_small = _make_subcategory(db, "Small", cat.id)
        sub_big = _make_subcategory(db, "Big", cat.id)
        _make_product(db, "BS1", "IS1", "P1", subcategory_id=sub_small.id)
        for i in range(4):
            _make_product(db, f"BB{i}", f"IB{i}", f"P{i+2}", subcategory_id=sub_big.id)

        resp = client.get(
            "/api/v1/subcategories?sort_by=product_count&sort_order=desc&include_counts=true"
        )
        subs = resp.json()["data"]["subcategories"]
        assert subs[0]["name"] == "Big"
        assert subs[0]["product_count"] == 4
        assert subs[1]["product_count"] == 1

    # ── Validation failures ────────────────────────────────────────────────────

    def test_limit_above_100_returns_400(self, client):
        resp = client.get("/api/v1/subcategories?limit=101")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_sort_by_returns_400(self, client):
        resp = client.get("/api/v1/subcategories?sort_by=nonexistent")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_sort_order_returns_400(self, client):
        resp = client.get("/api/v1/subcategories?sort_order=randomly")
        assert resp.status_code == 400

    def test_limit_100_is_allowed(self, client):
        resp = client.get("/api/v1/subcategories?limit=100")
        assert resp.status_code == 200

    def test_tenant_isolation_not_yet_implemented(self, client, db):
        """
        Placeholder: tenant isolation is not implemented because the `subcategories`
        table has no `business_id` column and no JWT auth middleware exists.

        TODO(auth): When business_id is added to the schema and JWT is wired up,
        replace this test with tenant isolation assertions.
        """
        pytest.skip("Tenant isolation requires business_id column + JWT auth middleware")


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/v1/subcategories/{id}  — DETAIL
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetSubcategory:

    def test_returns_subcategory_detail(self, client, db):
        cat = _make_category(db, "Skincare")
        sub = _make_subcategory(db, "Toner", cat.id, name_ar="تونر")

        resp = client.get(f"/api/v1/subcategories/{sub.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert data["id"] == sub.id
        assert data["name"] == "Toner"
        assert data["name_ar"] == "تونر"
        assert data["is_active"] is True

    def test_includes_parent_category(self, client, db):
        cat = _make_category(db, "Skincare")
        sub = _make_subcategory(db, "Toner", cat.id)

        resp = client.get(f"/api/v1/subcategories/{sub.id}")
        data = resp.json()["data"]
        assert data["category"]["id"] == cat.id
        assert data["category"]["name"] == "Skincare"

    def test_category_is_null_name_when_orphaned(self, client, db):
        """Subcategory with a category_id that doesn't exist → name is null."""
        sub = _make_subcategory(db, "Orphan", category_id=99999)
        resp = client.get(f"/api/v1/subcategories/{sub.id}")
        data = resp.json()["data"]
        assert data["category"]["id"] == 99999
        assert data["category"]["name"] is None

    def test_includes_correct_product_count(self, client, db):
        cat = _make_category(db, "Skincare")
        sub = _make_subcategory(db, "Toner", cat.id)
        _make_product(db, "B001", "IC001", "P1", subcategory_id=sub.id)
        _make_product(db, "B002", "IC002", "P2", subcategory_id=sub.id)
        _make_product(db, "B003", "IC003", "P3", subcategory_id=sub.id)

        resp = client.get(f"/api/v1/subcategories/{sub.id}")
        assert resp.json()["data"]["product_count"] == 3

    def test_product_count_excludes_other_subcategories(self, client, db):
        cat = _make_category(db, "Skincare")
        sub_a = _make_subcategory(db, "Toner", cat.id)
        sub_b = _make_subcategory(db, "Serum", cat.id)
        _make_product(db, "BA1", "IA1", "P1", subcategory_id=sub_a.id)
        _make_product(db, "BB1", "IB1", "P2", subcategory_id=sub_b.id)
        _make_product(db, "BB2", "IB2", "P3", subcategory_id=sub_b.id)

        resp = client.get(f"/api/v1/subcategories/{sub_a.id}")
        assert resp.json()["data"]["product_count"] == 1

    def test_product_count_zero_when_empty(self, client, db):
        cat = _make_category(db, "Skincare")
        sub = _make_subcategory(db, "Empty Toner", cat.id)
        resp = client.get(f"/api/v1/subcategories/{sub.id}")
        assert resp.json()["data"]["product_count"] == 0

    def test_not_found_returns_404(self, client):
        resp = client.get("/api/v1/subcategories/99999")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error"]["code"] == "NOT_FOUND"

    def test_invalid_id_type_returns_422(self, client):
        resp = client.get("/api/v1/subcategories/not-an-integer")
        assert resp.status_code == 422
