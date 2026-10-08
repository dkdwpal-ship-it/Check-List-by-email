"""Data models shared by the extraction and planning stages."""

from __future__ import annotations

from datetime import date
from datetime import date as Date  # alias: ChecklistItem has a field named `date`
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Recurrence(BaseModel):
    """A repeating task inferred from past mails (e.g. a weekly report every Friday)."""

    freq: Literal["daily", "weekly", "biweekly", "monthly"]
    weekday: Optional[int] = Field(
        default=None, description="0=월요일 ... 6=일요일 (weekly/biweekly)"
    )
    day_of_month: Optional[int] = Field(default=None, description="1-31 (monthly)")
    anchor_date: Optional[str] = Field(
        default=None, description="biweekly 기준이 되는 과거 발생일 YYYY-MM-DD"
    )


class Task(BaseModel):
    title: str = Field(description="해야 할 일을 동사로 끝나는 한 줄로 (예: 'Q3 실적 보고서 제출')")
    description: str = Field(default="", description="세부 내용, 산출물, 주의사항")
    due_date: Optional[str] = Field(default=None, description="마감일 YYYY-MM-DD, 없으면 null")
    due_text: str = Field(default="", description="메일 원문의 기한 표현 (예: '다음주 금요일까지')")
    requester: str = Field(default="", description="요청자 이름/메일")
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["open", "done"] = "open"
    recurrence: Optional[Recurrence] = None
    last_mail_date: Optional[str] = Field(
        default=None, description="이 업무와 관련된 가장 최근 메일의 발송일 YYYY-MM-DD"
    )
    source_message_ids: list[str] = Field(default_factory=list)
    source_subjects: list[str] = Field(default_factory=list)
    evidence: str = Field(default="", description="근거가 되는 메일 문장 인용 (짧게)")

    @field_validator("due_date", "last_mail_date")
    @classmethod
    def _valid_date(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        try:
            date.fromisoformat(v[:10])
        except ValueError:
            return None
        return v[:10]

    @property
    def due(self) -> Optional[date]:
        return date.fromisoformat(self.due_date) if self.due_date else None

    @property
    def last_seen(self) -> Optional[date]:
        return date.fromisoformat(self.last_mail_date) if self.last_mail_date else None


class TaskList(BaseModel):
    tasks: list[Task] = Field(default_factory=list)


class MailSummary(BaseModel):
    """메일 1건의 요약 (일자별 메일 요약 화면용)."""

    mail_id: str = Field(description="입력에 표시된 [메일 ID] 값 그대로 (예: M12)")
    summary: str = Field(description="핵심 내용 1~2문장 (누가 무엇을 언제까지/왜)")
    key_points: list[str] = Field(default_factory=list, description="중요 세부사항 최대 3개 (일정, 숫자, 결정사항)")
    keywords: list[str] = Field(
        default_factory=list,
        description="이 메일의 핵심 키워드 2~5개 (프로젝트명·고객사·제품·업무 주제 등 고유한 명사구)",
    )
    category: Literal["요청", "회의", "보고", "공지", "결재", "회신", "참고", "기타"] = "기타"
    needs_action: bool = Field(default=False, description="사용자가 해야 할 일이 있는 메일이면 true")
    importance: Literal["high", "medium", "low"] = "medium"


class PeriodOverview(BaseModel):
    period: str = Field(description="입력에 표시된 기간 값 그대로 (예: 2026-09)")
    overview: str = Field(description="그 기간 메일의 주요 흐름 2~3문장 (어떤 주제가 많았고 무엇이 진행/결정되었는지)")


class PeriodOverviews(BaseModel):
    periods: list[PeriodOverview] = Field(default_factory=list)


class Extraction(BaseModel):
    """메일 묶음 1회 분석 결과: 할 일 + 메일별 요약 (한 번의 LLM 호출로 함께 생성)."""

    tasks: list[Task] = Field(default_factory=list)
    summaries: list[MailSummary] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    task: Task
    date: Optional[Date] = None
    occurrence: bool = False  # True when generated from a recurrence rule


class Checklist(BaseModel):
    reference_date: date
    this_week: tuple[date, date]
    next_week: tuple[date, date]
    overdue: list[ChecklistItem] = Field(default_factory=list)
    this_week_items: list[ChecklistItem] = Field(default_factory=list)
    next_week_items: list[ChecklistItem] = Field(default_factory=list)
    undated: list[ChecklistItem] = Field(default_factory=list)
