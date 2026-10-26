import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.enums import PRDStatus

from .base import Base, TimestampMixin


class PRD(TimestampMixin, Base):
    __tablename__ = "prds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=PRDStatus.DRAFT)
    version: Mapped[int] = mapped_column(default=1)

    project = relationship("Project", back_populates="prds")
    tasks = relationship("Task", back_populates="prd")
    specs = relationship("Spec", back_populates="prd")

    __table_args__ = (
        CheckConstraint(
            status.in_([s.value for s in PRDStatus]),
            name="ck_prd_status",
        ),
    )
