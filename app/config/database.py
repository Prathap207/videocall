from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from urllib.parse import quote_plus

# Create DB if not exists
try:
    conn = psycopg2.connect(
        dbname="postgres",   # default DB
        user="postgres",
        password="root",
        host="localhost"
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM pg_database WHERE datname='live_streaming_app'")
    exists = cursor.fetchone()
    if not exists:
        cursor.execute("CREATE DATABASE live_streaming_app;")
        print("Database 'live_streaming_app' created.")
    else:
        print("Database 'live_streaming_app' already exists.")
    cursor.close()
    conn.close()
except Exception as e:
    print(f"Database creation failed: {e}")

# Database connection URL
password = quote_plus("root")
DATABASE_URL = f"postgresql://postgres:{password}@localhost:5432/demo"

engine = create_engine(DATABASE_URL,    pool_pre_ping=True, 
    pool_recycle=1800,)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()  # single Base for the whole app

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
