"""Shared SQLAlchemy types and constraints."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.domain.money import MAX_MINOR_UNITS


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ExactInteger(TypeDecorator[int]):
    """Do not let the ORM silently coerce binary floats into integer money."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and type(value) is not int:
            raise TypeError("Exact integer required; floats and booleans are forbidden")
        if value is not None and not -MAX_MINOR_UNITS - 1 <= value <= MAX_MINOR_UNITS:
            raise ValueError("Integer exceeds SQLite signed 64-bit capacity")
        return value


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


class Record:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_timestamp)
    # No default: every writer must explicitly state provenance.
    data_source: Mapped[str] = mapped_column(String(24))


def nonnegative_integer(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"typeof({column}) = 'integer' AND {column} >= 0",
        name=f"{column}_exact_nonnegative",
    )
