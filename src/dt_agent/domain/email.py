"""Email domain model (ADR-4, D-38, D-42, D-44, D-48).

Plain data produced by the Gmail adapter (task 15) and consumed by
`core/facts.py`, `core/routing.py`, `core/invitations.py` and the LLM
ports. No I/O and no decision-making here: header lookups such as
`auto_submitted` are convenience accessors, not policy.
"""

from collections.abc import Mapping

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class EmailAddress(BaseModel):
    """A display name plus an address, normalized for comparison.

    `address` is stored lowercase (Gmail addresses are case-insensitive
    for matching purposes, D-42/D-61); `name` keeps whatever casing the
    header carried.
    """

    model_config = ConfigDict(frozen=True)

    name: str = ""
    address: str

    @field_validator("address")
    @classmethod
    def _lowercase_address(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def domain(self) -> str:
        """The part of `address` after the last `@` (empty if there is none)."""
        _, _, domain_part = self.address.rpartition("@")
        return domain_part


class Attachment(BaseModel):
    """One MIME attachment, metadata only (the bytes aren't modeled here)."""

    model_config = ConfigDict(frozen=True)

    filename: str
    mime_type: str
    size: int = Field(ge=0)


class CalendarPart(BaseModel):
    """The raw iCalendar text of one `text/calendar` part or `.ics` attachment.

    Kept as raw text: parsing (UID, start, end, organizer, ...) is
    `core/invitations.py`'s job (D-48), not the domain model's.
    """

    model_config = ConfigDict(frozen=True)

    raw_ics: str


class Headers(BaseModel):
    """Case-insensitive lookup over an email's raw header lines.

    Header names are conventionally case-insensitive (RFC 5322); `get`
    hides that so callers never compare names by hand.
    """

    model_config = ConfigDict(frozen=True)

    values: Mapping[str, str] = Field(default_factory=dict)

    def get(self, name: str) -> str | None:
        target = name.casefold()
        for key, value in self.values.items():
            if key.casefold() == target:
                return value
        return None


class Email(BaseModel):
    """A Gmail message, already parsed into a provider-agnostic shape."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str
    label_ids: tuple[str, ...] = ()
    headers: Headers
    from_address: EmailAddress
    reply_to: EmailAddress | None = None
    to: tuple[EmailAddress, ...] = ()
    cc: tuple[EmailAddress, ...] = ()
    subject: str
    received_at: AwareDatetime
    text_body: str
    attachments: tuple[Attachment, ...] = ()
    calendar_parts: tuple[CalendarPart, ...] = ()

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup of one raw header value."""
        return self.headers.get(name)

    @property
    def auto_submitted(self) -> str | None:
        """The `Auto-Submitted` header value, if present (D-38, D-49)."""
        return self.header("Auto-Submitted")
