from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DISH_TYPES = ("dinner", "snack", "dessert", "baking", "side")
TYPE_LABELS = {
    "dinner": "Dinner",
    "snack": "Snack/savoury",
    "dessert": "Dessert",
    "baking": "Baking",
    "side": "Side",
}
PLATFORMS = ("instagram", "tiktok", "youtube", "web", "upload", "paper", "manual")
JOB_STATUSES = ("queued", "fetching", "transcribing", "extracting", "ready", "failed", "discarded")
BATCH_STATUSES = ("queued", "grouping", "done", "failed")


def utcnow() -> datetime:
    """Naive UTC (SQLite has no tz storage)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


dish_tags = Table(
    "dish_tags",
    Base.metadata,
    Column("dish_id", ForeignKey("dishes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class Dish(Base):
    __tablename__ = "dishes"
    __table_args__ = (
        CheckConstraint(_in("type", DISH_TYPES), name="ck_dish_type"),
        CheckConstraint(_in("source_platform", PLATFORMS), name="ck_dish_platform"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String, default="dinner")
    description: Mapped[str] = mapped_column(Text, default="")
    ingredients: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    servings: Mapped[int | None] = mapped_column(Integer)
    total_minutes: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text, unique=True)
    source_platform: Mapped[str] = mapped_column(String, default="manual")
    image_path: Mapped[str | None] = mapped_column(Text)
    scan_paths: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    tags: Mapped[list[Tag]] = relationship(secondary=dish_tags, order_by=Tag.name, lazy="selectin")

    @property
    def tag_names(self) -> list[str]:
        return [t.name for t in self.tags]


class MenuEntry(Base):
    __tablename__ = "menu_entries"
    __table_args__ = (
        CheckConstraint("kind IN ('dish', 'takeaway', 'none')", name="ck_menu_kind"),
        CheckConstraint("(kind = 'dish') = (dish_id IS NOT NULL)", name="ck_menu_dish"),
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    dish_id: Mapped[int | None] = mapped_column(ForeignKey("dishes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    dish: Mapped[Dish | None] = relationship(lazy="joined")


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (CheckConstraint(_in("status", BATCH_STATUSES), name="ck_batch_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    page_paths: Mapped[list] = mapped_column(JSON, default=list)
    unreadable_pages: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    jobs: Mapped[list["IngestJob"]] = relationship(back_populates="batch", order_by="IngestJob.id")


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (CheckConstraint(_in("status", JOB_STATUSES), name="ck_job_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    source_url: Mapped[str | None] = mapped_column(Text)
    user_text: Mapped[str] = mapped_column(Text, default="")
    upload_paths: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(String)
    raw: Mapped[dict | None] = mapped_column(JSON)
    extracted: Mapped[dict | None] = mapped_column(JSON)
    dish_id: Mapped[int | None] = mapped_column(ForeignKey("dishes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    batch: Mapped[ImportBatch | None] = relationship(back_populates="jobs")


class Calendar(Base):
    __tablename__ = "calendars"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    ics_url: Mapped[str] = mapped_column(Text, unique=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_since: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    events: Mapped[list["CalendarEvent"]] = relationship(
        back_populates="calendar", cascade="all, delete-orphan", passive_deletes=True
    )


class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"))
    uid: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(Text, default="")
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    start_at: Mapped[str | None] = mapped_column(Text)  # aware ISO-8601
    end_at: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)  # exclusive

    calendar: Mapped[Calendar] = relationship(back_populates="events")


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ShoppingList(Base):
    __tablename__ = "shopping_lists"
    __table_args__ = (UniqueConstraint("week_start"),)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    menu_fingerprint: Mapped[str | None] = mapped_column(String)
    pending_fingerprint: Mapped[str | None] = mapped_column(String)
    pending_since: Mapped[datetime | None] = mapped_column(DateTime)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    previous_data: Mapped[dict | None] = mapped_column(JSON)

    def clear_pending(self) -> None:
        self.pending_fingerprint = None
        self.pending_since = None
        self.failed_attempts = 0
        self.last_error = None
