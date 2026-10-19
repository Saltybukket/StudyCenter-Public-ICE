from datetime import date as Date, time as Time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=240)
    kind: Literal["task", "exam", "event", "registration", "notice"]
    identity_key: str | None = Field(default=None, max_length=200)
    # Only explicit identifiers (A2, Moodle activity id, exam id), never a guessed title.
    identity_scope: Literal["course", "document"] = "document"
    related_key: str | None = None
    semester: str | None = None
    group: str | None = None
    description: str = Field(default="", max_length=5000)
    date_meaning: Literal["deadline", "start", "window", "recommended", "none", "unclear"] = "none"
    date: Date | None = None
    time: Time | None = None
    end_date: Date | None = None
    end_time: Time | None = None
    obligation: Literal["required", "recommended", "optional", "unclear"] = "unclear"
    source_state: Literal["active", "cancelled", "unclear"] = "active"
    is_update: bool = False
    uncertainty: str | None = None
    quote: str = Field(min_length=3, max_length=1500)
    page: int = Field(ge=1)
    anchor: str = Field(min_length=1, max_length=240)

    @field_validator('date', 'end_date', mode='before')
    @classmethod
    def iso_dates(cls, value):
        import re
        if value is not None and not isinstance(value, Date):
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Datum muss ISO YYYY-MM-DD sein')
        return value

    @model_validator(mode="after")
    def date_precision(self):
        if self.time and not self.date:
            raise ValueError("Uhrzeit ohne Datum")
        if self.end_time and not self.end_date:
            raise ValueError("Endzeit ohne Enddatum")
        if self.end_date and not self.date:
            raise ValueError("Enddatum ohne Anfang")
        if self.end_date and self.date and self.end_date < self.date:
            raise ValueError("Ende liegt vor Beginn")
        if self.date == self.end_date and self.time and self.end_time and self.end_time < self.time:
            raise ValueError("Endzeit liegt vor Beginn")
        if (self.time and self.time.tzinfo) or (self.end_time and self.end_time.tzinfo):
            raise ValueError("Uhrzeiten sind lokale Europe/Vienna-Uhrzeiten")
        if self.kind == "registration" and self.date_meaning not in ("window", "unclear", "none"):
            raise ValueError("Anmeldung ist ein eigenes Fenster")
        if self.obligation == "recommended" and self.date_meaning == "deadline":
            raise ValueError("Empfehlung ist keine Abgabefrist")
        if self.identity_scope == "course" and not self.identity_key:
            raise ValueError("Kursidentität braucht eine ausdrückliche Kennung")
        return self


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Candidate]
