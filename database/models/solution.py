import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database.enums import SolutionOutcome

from .base import Base, TimestampMixin


class Solution(TimestampMixin, Base):
    __tablename__ = "solutions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    error_id: Mapped[str] = mapped_column(String(36), ForeignKey("errors.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    approach: Mapped[str] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(64), default=SolutionOutcome.UNKNOWN)

    __table_args__ = (
        CheckConstraint(
            outcome.in_([s.value for s in SolutionOutcome]),
            name="ck_solution_outcome",
        ),
    )
