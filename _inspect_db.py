import time as _time
import sys
sys.path.insert(0, r"D:\Kishor\Projects\switch2onlin677\AI")
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine, text
pg = create_engine("postgresql://postgres:admin123@localhost:5432/simple_test_db")
check_bcs = ["KILOUSECASE03", "TST001NEWBC", "100101", "3605521159991", "KIL0TEST01"]
query = f"SELECT barcode, item_code, item_name FROM products WHERE barcode IN ({','.join([':'+bc for bc in check_bcs])})"
with pg.connect() as c:
    rows = c.execute(text(query), {bc: bc for bc in check_bcs}).fetchall()
    print("Barcode state:")
    for r in rows:
        print(f"  {r[0]!r:30s}  item_code={r[1]!r:25s}  name={r[2][:45]}")
