from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Text
from app.database import Base
from app.models.tenant import utc_now


class SystemConfig(Base):
    __tablename__ = "system_configs"

    key = Column(String(64), primary_key=True, index=True)
    value = Column(Text, nullable=False, default="")
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
