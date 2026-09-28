"""Code-extracted facts passed to the classifier (D-42, D-44, D-61).

`Facts` is deliberately free-form here: `core/facts.py` (task 11) is the
only place that decides which keys exist and what they mean (SPF/DKIM/
DMARC, sender mismatch, bulk/link/attachment cues, the allowlist match).
This port only needs a typed, immutable container the classifier can
pass to the LLM (in a trusted section, separate from the untrusted
email content, D-44) and the audit log can render.
"""

from pydantic import BaseModel, ConfigDict


class Facts(BaseModel):
    """One fact per entry, e.g. `{"spf_pass": True, "bulk_cues": ["List-Unsubscribe present"]}`."""

    model_config = ConfigDict(frozen=True)

    items: dict[str, str | bool | int | list[str]] = {}

    def summary_lines(self) -> list[str]:
        """One `"key: value"` line per fact, for the classifier prompt and the audit log."""
        lines: list[str] = []
        for key, value in self.items.items():
            rendered = ", ".join(value) if isinstance(value, list) else str(value)
            lines.append(f"{key}: {rendered}")
        return lines
