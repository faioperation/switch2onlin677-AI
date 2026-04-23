from sqlalchemy import text
from database import engine, Base
import models

def init_db():
    print("Initializing database with fuzzy search capabilities...")
    with engine.connect() as conn:
        # Enable pg_trgm extension for fuzzy search
        print("Enabling pg_trgm extension...")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
        conn.commit()

    # Create all tables (Product table)
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        # Create Trigram Index for fuzzy matching on item_name
        print("Creating trigram index for item_name...")
        # Note: We use execute(text(...)) for raw SQL
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_products_item_name_trgm ON products USING gin (item_name gin_trgm_ops);"))
        conn.commit()
    
    print("Database initialization complete.")

if __name__ == "__main__":
    init_db()
