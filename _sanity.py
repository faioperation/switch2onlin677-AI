"""Quick probes of current PUT endpoint state."""
import time, sys
sys.path.insert(0, r"D:\Kishor\Projects\switch2onlin677\AI")
sys.stdout.reconfigure(encoding='utf-8')

# Patch env BEFORE importing main (imports engine at module level)
import os
os.environ["DATABASE_URL"] = "postgresql://postgres:admin123@localhost:5432/simple_test_db"

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
BC_SRC  = "KILOUSECASE03"   # supposed to exist
BC_CONFL= "3605521159991"   # known-clean conflict target
BC_NEW  = f"SANITY{int(time.time())}"  # guaranteed fresh

import json

def pp(label):
    bc = BC_SRC if "SRC" in label else BC_NEW
    r = client.get(f"/products/{bc}")
    print(f"\n{label} GET /products/{bc}")
    print(f"  status: {r.status_code}")
    body = r.json()
    if r.status_code == 200:
        print(f"  item_name : {body['data']['item_name']}")
        print(f"  item_code : {body['data']['item_code']}")
        print(f"  barcode   : {body['data']['barcode']}")
    else:
        print(f"  body: {json.dumps(body, ensure_ascii=False)[:120]}")

pp("A: GET BC_SRC")
pp("B: GET BC_NEW")

# Probe the rename
print(f"\nC: PUT rename {BC_SRC} -> {BC_NEW}")
r = client.put(f"/products/{BC_SRC}", json={"barcode": BC_NEW, "item_name": "SANITY-RENAME"})
print(f"  status: {r.status_code}")
body = r.json()
print(f"  body  : {json.dumps(body, ensure_ascii=False)[:250]}")

pp("D: GET BC_SRC after rename")
pp("E: GET BC_NEW after rename")

# Check productsearchindex
from sqlalchemy import create_engine, text
pg = create_engine("postgresql://postgres:admin123@localhost:5432/simple_test_db")
with pg.connect() as c:
    for bc in [BC_SRC, BC_NEW, "100101"]:
        row = c.execute(text(
            "SELECT product_id, item_code, item_name FROM productsearchindex WHERE product_id=:bc"
        ), {"bc": bc}).first()
        found = row is not None
        print(f"\nF: productsearchindex[{bc}]")
        print(f"  exists: {found}")
        if found:
            print(f"  keys  : id={row[0]!r}  code={row[1]!r}  name={row[2]!r}")
