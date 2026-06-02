import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
del _os, _sys

from sqlalchemy import text
from database import engine, Base
import models

def init_db():
    print("Initializing production database with AI-optimized search...")
    
    # Check if database is SQLite or PostgreSQL
    is_sqlite = engine.url.drivername == "sqlite"
    
    if not is_sqlite:
        with engine.connect() as conn:
            # Enable pg_trgm extension for fuzzy search (PostgreSQL only)
            print("Enabling pg_trgm extension...")
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
            conn.commit()
    else:
        print("Warning: Running on SQLite. Trigram fuzzy search will NOT be available.")

    # Create all tables
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)

    if not is_sqlite:
        with engine.connect() as conn:
            # Create Trigram Index for fuzzy matching on 'item_name'
            print("Creating trigram index for item_name...")
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_products_item_name_trgm ON products USING gin (item_name gin_trgm_ops);"))
            
            # Create GIN index for JSON fields (concerns/tags) for fast filtering
            print("Creating GIN indexes for AI intelligence fields...")
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_products_concerns ON products USING gin (concerns);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_products_tags ON products USING gin (tags);"))
            
            # Regular B-tree indexes for exact matches
            print("Creating B-tree indexes for brand and category...")
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_products_brand ON products (brand);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);"))
            
            conn.commit()
    
    print("Database initialization complete.")

if __name__ == "__main__":
    init_db()
