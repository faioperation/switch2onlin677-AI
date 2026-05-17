"""
tests/test_brands.py
====================
Unit and integration tests for:
  GET /api/v1/brands
  GET /api/v1/brands/{id}

Run with:
    pytest tests/test_brands.py -v
"""

import pytest
from models import Brand, Category, Product


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_brand(db, name, is_active=1, name_ar=None):
    b = Brand(name=name, name_ar=name_ar, is_active=is_active)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _make_category(db, name):
    c = Category(name=name, is_active=1)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_product(db, barcode, item_code, item_name, brand_id=None, category_id=None, subcategory_id=None):
    p = Product(
        barcode=barcode,
        item_code=item_code,
        item_name=item_name,
        brand_id=brand_id,
        category_id=category_id,
        subcategory_id=subcategory_id,
    )
    db.add(p)
    db.commit()
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/v1/brands  — LIST
# ═══════════════════════════════════════════════════════════════════════════════

class TestListBrands:

    def test_empty_database_returns_success(self, client):
        resp = client.get("/api/v1/brands")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["brands"] == []
        assert body["data"]["pagination"]["total"] == 0

    def test_returns_all_brands(self, client, db):
        _make_brand(db, "NYX")
        _make_brand(db, "L'Oreal")
        _make_brand(db, "MAC")

        resp = client.get("/api/v1/brands")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["pagination"]["total"] == 3
        assert len(data["brands"]) == 3

    def test_pagination_page_2(self, client, db):
        """25 brands, page=2&limit=10 → 10 items, correct pagination meta."""
        for i in range(25):
            _make_brand(db, f"Brand {i:02d}")

        resp = client.get("/api/v1/brands?page=2&limit=10")
        assert resp.status_code == 200
        data = resp.json()["data"]
        pg = data["pagination"]
        assert pg["total"] == 25
        assert pg["page"] == 2
        assert pg["limit"] == 10
        assert pg["total_pages"] == 3
        assert pg["has_prev"] is True
        assert pg["has_next"] is True
        assert len(data["brands"]) == 10

    def test_search_partial_match(self, client, db):
        _make_brand(db, "NYX Professional")
        _make_brand(db, "L'Oreal")
        _make_brand(db, "NYX Cosmetics")

        resp = client.get("/api/v1/brands?search=NYX")
        assert resp.status_code == 200
        names = [b["name"] for b in resp.json()["data"]["brands"]]
        assert "NYX Professional" in names
        assert "NYX Cosmetics" in names
        assert "L'Oreal" not in names

    def test_search_case_insensitive(self, client, db):
        _make_brand(db, "NYX")
        resp = client.get("/api/v1/brands?search=nyx")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["brands"]) == 1

    def test_is_active_filter(self, client, db):
        _make_brand(db, "Active Brand", is_active=1)
        _make_brand(db, "Inactive Brand", is_active=0)

        resp = client.get("/api/v1/brands?is_active=true")
        names = [b["name"] for b in resp.json()["data"]["brands"]]
        assert "Active Brand" in names
        assert "Inactive Brand" not in names

    def test_category_id_filter_returns_only_brands_with_products_in_category(self, client, db):
        """category_id filter: only brands that have products in the specified category."""
        brand_a = _make_brand(db, "Brand A")
        brand_b = _make_brand(db, "Brand B")
        cat_x = _make_category(db, "Skincare")
        cat_y = _make_category(db, "Perfume")

        # brand_a has product in cat_x; brand_b has product in cat_y
        _make_product(db, "BA1", "IA1", "P1", brand_id=brand_a.id, category_id=cat_x.id)
        _make_product(db, "BB1", "IB1", "P2", brand_id=brand_b.id, category_id=cat_y.id)

        resp = client.get(f"/api/v1/brands?category_id={cat_x.id}")
        assert resp.status_code == 200
        names = [b["name"] for b in resp.json()["data"]["brands"]]
        assert "Brand A" in names
        assert "Brand B" not in names

    def test_category_id_filter_empty_when_no_match(self, client, db):
        """category_id filter with no products → empty result."""
        _make_brand(db, "Brand A")
        cat = _make_category(db, "Skincare")
        # No products exist for this category

        resp = client.get(f"/api/v1/brands?category_id={cat.id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["brands"] == []

    def test_include_counts_attaches_product_count(self, client, db):
        brand = _make_brand(db, "NYX")
        _make_product(db, "B001", "IC001", "P1", brand_id=brand.id)
        _make_product(db, "B002", "IC002", "P2", brand_id=brand.id)

        resp = client.get("/api/v1/brands?include_counts=true")
        assert resp.status_code == 200
        brands = resp.json()["data"]["brands"]
        assert brands[0]["product_count"] == 2

    def test_include_counts_false_omits_count_field(self, client, db):
        _make_brand(db, "NYX")
        resp = client.get("/api/v1/brands")
        brand = resp.json()["data"]["brands"][0]
        assert "product_count" not in brand

    def test_sort_by_name_asc(self, client, db):
        _make_brand(db, "Zzz Brand")
        _make_brand(db, "Aaa Brand")
        _make_brand(db, "Mmm Brand")

        resp = client.get("/api/v1/brands?sort_by=name&sort_order=asc")
        names = [b["name"] for b in resp.json()["data"]["brands"]]
        assert names == sorted(names)

    def test_sort_by_product_count_desc(self, client, db):
        """sort_by=product_count&sort_order=desc → most products first."""
        brand_a = _make_brand(db, "Small Brand")
        brand_b = _make_brand(db, "Big Brand")
        _make_product(db, "BA1", "IA1", "P1", brand_id=brand_a.id)
        for i in range(5):
            _make_product(db, f"BB{i}", f"IB{i}", f"P{i+2}", brand_id=brand_b.id)

        resp = client.get(
            "/api/v1/brands?sort_by=product_count&sort_order=desc&include_counts=true"
        )
        brands = resp.json()["data"]["brands"]
        assert brands[0]["name"] == "Big Brand"
        assert brands[0]["product_count"] == 5
        assert brands[1]["product_count"] == 1

    # ── Validation failures ────────────────────────────────────────────────────

    def test_limit_above_100_returns_400(self, client):
        resp = client.get("/api/v1/brands?limit=101")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_sort_by_returns_400(self, client):
        resp = client.get("/api/v1/brands?sort_by=bad_column")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_sort_order_returns_400(self, client):
        resp = client.get("/api/v1/brands?sort_order=upward")
        assert resp.status_code == 400

    def test_limit_100_is_allowed(self, client):
        resp = client.get("/api/v1/brands?limit=100")
        assert resp.status_code == 200

    def test_tenant_isolation_not_yet_implemented(self, client, db):
        """
        Placeholder: tenant isolation is not implemented because the `brands`
        table has no `business_id` column and no JWT auth middleware exists.

        TODO(auth): When business_id is added to the schema and JWT is wired up,
        replace this test with tenant isolation assertions.
        """
        pytest.skip("Tenant isolation requires business_id column + JWT auth middleware")


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/v1/brands/{id}  — DETAIL
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetBrand:

    def test_returns_brand_detail(self, client, db):
        brand = _make_brand(db, "NYX", name_ar="نيكس")
        resp = client.get(f"/api/v1/brands/{brand.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert data["id"] == brand.id
        assert data["name"] == "NYX"
        assert data["name_ar"] == "نيكس"
        assert data["is_active"] is True

    def test_includes_product_count(self, client, db):
        brand = _make_brand(db, "NYX")
        _make_product(db, "B001", "IC001", "P1", brand_id=brand.id)
        _make_product(db, "B002", "IC002", "P2", brand_id=brand.id)

        resp = client.get(f"/api/v1/brands/{brand.id}")
        assert resp.json()["data"]["product_count"] == 2

    def test_product_count_excludes_other_brands(self, client, db):
        brand_a = _make_brand(db, "A")
        brand_b = _make_brand(db, "B")
        _make_product(db, "BA1", "IA1", "P1", brand_id=brand_a.id)
        _make_product(db, "BB1", "IB1", "P2", brand_id=brand_b.id)
        _make_product(db, "BB2", "IB2", "P3", brand_id=brand_b.id)

        resp = client.get(f"/api/v1/brands/{brand_a.id}")
        assert resp.json()["data"]["product_count"] == 1

    def test_includes_category_breakdown(self, client, db):
        """category_breakdown lists categories this brand has products in."""
        brand = _make_brand(db, "NYX")
        cat_a = _make_category(db, "Lipstick")
        cat_b = _make_category(db, "Foundation")

        _make_product(db, "B001", "IC001", "P1", brand_id=brand.id, category_id=cat_a.id)
        _make_product(db, "B002", "IC002", "P2", brand_id=brand.id, category_id=cat_a.id)
        _make_product(db, "B003", "IC003", "P3", brand_id=brand.id, category_id=cat_b.id)

        resp = client.get(f"/api/v1/brands/{brand.id}")
        data = resp.json()["data"]
        breakdown = {row["name"]: row["product_count"] for row in data["category_breakdown"]}
        assert breakdown["Lipstick"] == 2
        assert breakdown["Foundation"] == 1

    def test_category_breakdown_empty_for_no_products(self, client, db):
        brand = _make_brand(db, "NYX")
        resp = client.get(f"/api/v1/brands/{brand.id}")
        assert resp.json()["data"]["category_breakdown"] == []
        assert resp.json()["data"]["product_count"] == 0

    def test_not_found_returns_404(self, client):
        resp = client.get("/api/v1/brands/99999")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error"]["code"] == "NOT_FOUND"

    def test_invalid_id_type_returns_422(self, client):
        resp = client.get("/api/v1/brands/not-an-integer")
        assert resp.status_code == 422
