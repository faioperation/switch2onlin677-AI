import sys; sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine, text
pg = create_engine("postgresql://postgres:admin123@localhost:5432/simple_test_db")
with pg.connect() as c:
    for bc in ["3605521159991", "100102", "KILOUSECASE03"]:
        r = c.execute(text("SELECT barcode, item_code, item_name FROM products WHERE barcode=:bc"), {"bc": bc}).first()
        if r:
            print(f"  {bc!r:25s} found=True  item_code={r[1]!r:27s}  name={str(r[2])[:45]}")
        else:
            print(f"  {bc!r:25s} found=False")
