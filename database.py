from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

load_dotenv() 


# For local development use SQLite, for production use DATABASE_URL if provided
database_url = os.getenv("DATABASE_URL")

if database_url:
    # Production: PostgreSQL or other database via DATABASE_URL
    engine = create_engine(database_url)
    # print(f"Using database: {database_url}")
    print("DhifafBot Running................You Can Chat with DhifafBot.............")
else:
    # Development: SQLite
    engine = create_engine(
        "sqlite:///./shopbot.db",
        connect_args={"check_same_thread": False}
    )
    print("Using SQLite database: ./shopbot.db")

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
