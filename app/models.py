from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(300))
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    products: Mapped[list["Product"]] = relationship(back_populates="store", cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "url", name="uq_product_store_url"),
        Index("ix_product_store_active", "store_id", "is_active"),
        Index("ix_product_price", "price"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    name: Mapped[str] = mapped_column(String(500))
    search_text: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(500))
    price: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    length_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    width_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    thickness_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_dimensions_text: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Фасовка и цена за единицу — без них сравнение "где дешевле" сравнивает
    # пакет 2 кг с мешком 50 кг. Заполняются из названия товара (app/matching.py).
    pack_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pack_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Товар пропал из каталога магазина: из выдачи убираем, но историю не теряем.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # Время прогона, в котором товар был последний раз виден на сайте (наивный UTC).
    scraped_at: Mapped[datetime] = mapped_column(DateTime)

    store: Mapped["Store"] = relationship(back_populates="products")
