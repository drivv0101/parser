import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.logging_config import get_logger
from app.models import Base

logger = get_logger("db")

DB_PATH = Path(os.getenv("STROY_DB_PATH") or Path(__file__).resolve().parent.parent / "prices.db")
engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)

# Колонки, добавленные после первого релиза. SQLite умеет ADD COLUMN мгновенно,
# полноценная миграционная система (alembic) для одной таблицы избыточна.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "products": [
        ("pack_value", "FLOAT"),
        ("pack_unit", "VARCHAR(10)"),
        ("unit_price", "FLOAT"),
        ("is_active", "BOOLEAN NOT NULL DEFAULT 1"),
        ("in_stock", "BOOLEAN"),
        ("stock_note", "VARCHAR(120)"),
    ],
}

_ADDED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_product_store_active ON products (store_id, is_active)",
    "CREATE INDEX IF NOT EXISTS ix_product_price ON products (price)",
]


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _):
    # WAL позволяет читать (веб-приложение) без блокировок, пока фоновый скрапер пишет в базу.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _apply_migrations() -> None:
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl_type in columns:
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
                    logger.info("миграция: %s.%s добавлена", table, name)
        # create_all() пропускает уже существующие таблицы целиком, поэтому индексы,
        # добавленные позже, создаём отдельно.
        for statement in _ADDED_INDEXES:
            connection.execute(text(statement))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _apply_migrations()


def get_session() -> Session:
    return SessionLocal()
