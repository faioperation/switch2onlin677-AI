import pandas as pd
import os
import argparse
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal, engine
from models import Product, Base
from dotenv import load_dotenv

load_dotenv()

def clean_tags(row):
    """Combine Tag_EN, Tag_MSA, and Tag_IRQ into a single list."""
    tags = []
    for col in ['Tag_EN', 'Tag_MSA', 'Tag_IRQ']:
        val = row.get(col)
        if pd.notna(val) and str(val).strip() != "":
            parts = [p.strip() for p in str(val).split(",") if p.strip()]
            tags.extend(parts)
    return list(set(tags))

def import_catalog(file_path):
    if not os.path.exists(file_path):
        print(f"Error: File not found at {file_path}")
        return

    print(f"Loading catalog from: {file_path}")
    try:
        df = pd.read_excel(file_path)
    except Exception as e:
        print(f"Error reading Excel file: {e}")
        return

    print(f"Total records found in file: {len(df)}")
    
    # Detect Price column (case-insensitive)
    price_col = next((c for c in df.columns if 'price' in c.lower()), None)
    if price_col:
        print(f"Detected Price column: '{price_col}'")
    else:
        print("No Price column detected in Excel.")

    db = SessionLocal()
    success_count = 0
    error_count = 0

    print("Starting Upsert process...")

    for index, row in df.iterrows():
        try:
            barcode_raw = row.get('Bar Code')
            if pd.isna(barcode_raw) or str(barcode_raw).strip() == "" or str(barcode_raw).strip().lower() == "nan":
                error_count += 1
                continue

            # Normalize barcode: convert to string and remove leading zeros
            barcode = str(barcode_raw).strip().lstrip('0')
            if barcode == "": 
                barcode = "0"

            # Handle Price from Excel if it exists
            raw_price = row.get(price_col) if price_col else 0.0
            try:
                price_val = float(raw_price) if pd.notna(raw_price) else 0.0
            except:
                price_val = 0.0

            data = {
                "barcode": barcode,
                "item_code": str(row.get('Item No.')).strip() if pd.notna(row.get('Item No.')) else None,
                "item_name": str(row.get('Item Description')).strip() if pd.notna(row.get('Item Description')) else "Unknown Product",
                "brand": str(row.get('Brand')).strip() if pd.notna(row.get('Brand')) else None,
                "category": str(row.get('Category')).strip() if pd.notna(row.get('Category')) else None,
                "description": str(row.get('Item Description')).strip() if pd.notna(row.get('Item Description')) else None,
                "image_url": str(row.get('Image_URL')).strip() if pd.notna(row.get('Image_URL')) else None,
                "price": price_val,
                "tags": clean_tags(row),
                "concerns": [],
                "skin_type": None
            }

            stmt = insert(Product).values(**data)
            update_dict = {k: v for k, v in data.items() if k != "barcode"}
            stmt = stmt.on_conflict_do_update(index_elements=['barcode'], set_=update_dict)
            
            db.execute(stmt)
            success_count += 1
            
            if success_count % 1000 == 0:
                print(f"Processed {success_count} items...")

        except Exception as e:
            print(f"Error at row {index+2}: {str(e)}")
            error_count += 1

    db.commit()
    db.close()
    print(f"\nImport Complete! Success: {success_count}, Failed/Skipped: {error_count}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Master Catalog from Excel")
    parser.add_argument("--file", required=True, help="Path to the Excel file")
    args = parser.parse_args()
    Base.metadata.create_all(bind=engine)
    import_catalog(args.file)
