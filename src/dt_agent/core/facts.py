"""Fact extraction for phishing/bulk/trust signals (D-38, D-39, D-41, D-42, D-44, D-56, D-61).

Deterministic code extracts facts from an email's headers, sender, links
and attachments and hands them to the classification LLM in a trusted
section, next to the untrusted email content (D-44). No code rule forces
a label; only the LLM decides. Links are analysed as text only, never
fetched; attachments are never opened.

This module defines its own result type, :class:`EmailFacts`; it does not
depend on `dt_agent.ports` (that package is owned by another task).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from dt_agent.domain.email import Email

# -- Automated-sender guard (shared D-38/D-39) -------------------------------

_AUTOMATED_LOCAL_PARTS = frozenset({"no-reply", "noreply", "mailer-daemon"})
_BULK_PRECEDENCE_VALUES = frozenset({"bulk", "list", "junk"})

# -- Sender impersonation (D-44) ---------------------------------------------

_BRAND_WORDS = (
    "paypal",
    "amazon",
    "apple",
    "microsoft",
    "netflix",
    "dhl",
    "fedex",
    "ups",
    "visa",
    "mastercard",
    "irs",
)
_DOMAIN_LIKE_RE = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})\b", re.IGNORECASE)
_EMAIL_LIKE_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# -- Links (D-44) -------------------------------------------------------------

# Matches the `text (href)` shape produced by `core/html_text.html_to_text`
# (and usable directly in a plain-text body): optional preceding text, then
# an http(s) URL in parentheses. Only this shape is used for the "link text
# looks like a domain" check, since it needs visible link text to compare.
_LINK_RE = re.compile(r"(?:([^\n()]+?)\s+)?\((?P<url>https?://[^\s()]+)\)")
# Any http(s) URL at all, parenthesized or bare, for the host-based checks
# (IP / shortener / punycode / credentials) and the link-host listing below.
_URL_RE = re.compile(r"https?://[^\s()<>]+")
_BARE_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.IGNORECASE)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SHORTENERS = frozenset(
    {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly"}
)

# -- Attachments (D-44) -------------------------------------------------------

_RISKY_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".zip",
        ".rar",
        ".7z",
        ".exe",
        ".scr",
        ".js",
        ".vbs",
        ".bat",
        ".cmd",
        ".msi",
        ".docm",
        ".xlsm",
        ".pptm",
    }
)

# -- Bulk cues (D-44) ---------------------------------------------------------

_BULK_BODY_PHRASES = ("unsubscribe", "view in browser", "view this email in your browser")

# -- Public free-mail providers (task 52) -------------------------------------

# Domains anyone can register an address at; a sender on one of these carries no
# organizational identity of its own (D-44's observation only, not a verdict).
_FREE_MAIL_DOMAINS = (
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "gmx.com",
    "gmx.net",
    "mail.com",
    "mail.ru",
    "yandex.com",
    "yandex.ru",
    "zoho.com",
)


@dataclass(frozen=True, slots=True)
class AuthResult:
    """SPF/DKIM/DMARC results parsed from Gmail's `Authentication-Results`.

    `dkim_domain` and `dmarc_domain` are the `header.d=` / `header.from=`
    values (D-42): the domain each mechanism actually authenticated,
    which is what anti-spoofing checks compare against the sender's
    `From` domain.
    """

    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None
    dkim_domain: str | None = None
    dmarc_domain: str | None = None


@dataclass(frozen=True, slots=True)
class LinkIssue:
    """One suspicious link found in the email body (D-44)."""

    href: str
    reason: str


@dataclass(frozen=True, slots=True)
class EmailFacts:
    """Deterministic facts extracted from one email, for the LLM's trusted section.

    Every field is always populated (booleans default False, collections
    default empty); only the facts that actually fired are rendered by
    :meth:`to_prompt_lines`.
    """

    sender_domain: str
    auth: AuthResult

    internal: bool
    internal_reason: str | None

    allowlisted: bool
    allowlist_reason: str | None

    automated_reasons: tuple[str, ...]

    reply_to_domain: str | None
    reply_to_mismatch: bool
    impersonation_reason: str | None

    link_issues: tuple[LinkIssue, ...]
    link_hosts: tuple[str, ...]
    risky_attachments: tuple[str, ...]
    bulk_cues: tuple[str, ...]
    gmail_category_labels: tuple[str, ...]

    free_mail_sender: bool
    calendar_invitation: bool

    @property
    def automated(self) -> bool:
        """Whether the shared automated-sender guard fired (D-38/D-39)."""
        return bool(self.automated_reasons)

    def to_prompt_lines(self) -> list[str]:
        """Render only the facts that fired, for the LLM's trusted section.

        Exactly one sender-authentication line is always present (D-44): a positive
        pass line, the existing failure line, or a line naming the header's total
        absence -- observations only, no interpretation (task 52, D-44).
        """
        lines: list[str] = []

        domain_passed, _ = _domain_auth_passed(self.auth, self.sender_domain)
        results = (self.auth.spf, self.auth.dkim, self.auth.dmarc)
        results_present = any(result is not None for result in results)
        if domain_passed:
            passed_checks: list[str] = []
            if self.auth.spf == "pass":
                passed_checks.append("SPF")
            if self.auth.dkim == "pass" and self.auth.dkim_domain == self.sender_domain:
                passed_checks.append("DKIM")
            if self.auth.dmarc == "pass" and self.auth.dmarc_domain == self.sender_domain:
                passed_checks.append("DMARC")
            checks_text = _join_and(passed_checks)
            verb = "pass" if len(passed_checks) > 1 else "passes"
            lines.append(
                f"Sender authentication: {checks_text} {verb} for {self.sender_domain}: "
                "the From domain is not forged."
            )
        elif results_present:
            lines.append(
                f"Authentication: spf={self.auth.spf}, dkim={self.auth.dkim}, "
                f"dmarc={self.auth.dmarc}"
            )
        else:
            lines.append("Sender authentication: no Authentication-Results header.")

        if self.internal:
            lines.append(
                f"Sender domain ({self.sender_domain}) is internal; authentication passed."
            )
        if self.internal_reason:
            lines.append(self.internal_reason)

        if self.allowlisted:
            lines.append(f"Sender ({self.sender_domain}) is on the trusted-sender allowlist.")
        if self.allowlist_reason:
            lines.append(self.allowlist_reason)

        if self.free_mail_sender:
            lines.append(
                f"Sender domain {self.sender_domain} is a public free-mail provider "
                "(anyone can register an address there)."
            )

        if self.automated_reasons:
            lines.append("Automated sender: " + "; ".join(self.automated_reasons))

        if self.reply_to_mismatch:
            lines.append(
                f"Reply-To domain ({self.reply_to_domain}) differs from the From domain "
                f"({self.sender_domain})."
            )
        if self.impersonation_reason:
            lines.append(f"Possible sender impersonation: {self.impersonation_reason}")

        for issue in self.link_issues:
            lines.append(f"Suspicious link ({issue.href}): {issue.reason}")

        if self.link_hosts:
            hosts = list(self.link_hosts)
            shown = hosts[:10]
            more = f", +{len(hosts) - 10} more" if len(hosts) > 10 else ""
            lines.append(
                f"Sender domain: {self.sender_domain}. Link hosts: {', '.join(shown)}{more}"
            )

        if self.risky_attachments:
            lines.append("Risky attachment(s): " + ", ".join(self.risky_attachments))

        if self.bulk_cues:
            lines.append("Bulk-mail cue(s): " + "; ".join(self.bulk_cues))

        if self.calendar_invitation:
            lines.append("Contains a calendar invitation part (.ics).")

        if self.gmail_category_labels:
            lines.append("Gmail category label(s): " + ", ".join(self.gmail_category_labels))

        return lines


def _join_and(items: Sequence[str]) -> str:
    """`"A"`, `"A and B"`, or `"A, B and C"` — used for the positive-authentication line."""
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def truncate_for_llm(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate `text` to `max_chars` (D-56). Returns `(text, truncated)`."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _parse_authentication_results(value: str | None) -> AuthResult:
    """Parse Gmail's `Authentication-Results` header (shared D-42/D-61)."""
    if not value:
        return AuthResult()

    spf = dkim = dmarc = None
    dkim_domain = dmarc_domain = None
    for segment in value.split(";"):
        mech_match = re.search(r"\b(spf|dkim|dmarc)=(\w+)", segment, re.IGNORECASE)
        if not mech_match:
            continue
        mechanism, result = mech_match.group(1).lower(), mech_match.group(2).lower()
        if mechanism == "spf":
            spf = result
        elif mechanism == "dkim":
            dkim = result
            domain_match = re.search(r"header\.d=([^\s;)]+)", segment, re.IGNORECASE)
            if domain_match:
                dkim_domain = domain_match.group(1).rstrip(".").lower()
            else:
                # Some signers omit `header.d=`; fall back to the domain of
                # the signing identity (`header.i=@domain`).
                identity_match = re.search(
                    r"header\.i=[^@\s;)]*@([^\s;)]+)", segment, re.IGNORECASE
                )
                if identity_match:
                    dkim_domain = identity_match.group(1).rstrip(".").lower()
        else:  # dmarc
            dmarc = result
            domain_match = re.search(r"header\.from=([^\s;)]+)", segment, re.IGNORECASE)
            if domain_match:
                dmarc_domain = domain_match.group(1).rstrip(".").lower()

    return AuthResult(
        spf=spf, dkim=dkim, dmarc=dmarc, dkim_domain=dkim_domain, dmarc_domain=dmarc_domain
    )


def _domain_auth_passed(auth: AuthResult, domain: str) -> tuple[bool, str | None]:
    """Whether DKIM or DMARC passes *for this domain* (D-42/D-61).

    Returns `(passed, reason)`; `reason` names the failing check(s) and is
    set only when `passed` is False.
    """
    if (auth.dkim == "pass" and auth.dkim_domain == domain) or (
        auth.dmarc == "pass" and auth.dmarc_domain == domain
    ):
        return True, None

    failures: list[str] = []
    if auth.dkim is not None:
        suffix = "" if auth.dkim_domain == domain else " (different domain)"
        failures.append(f"dkim={auth.dkim}{suffix}")
    if auth.dmarc is not None:
        suffix = "" if auth.dmarc_domain == domain else " (different domain)"
        failures.append(f"dmarc={auth.dmarc}{suffix}")
    if not failures:
        failures.append("no dkim/dmarc result for this domain")
    return False, ", ".join(failures)


def _automated_sender_reasons(email: Email, own_address: str) -> tuple[str, ...]:
    """The shared automated-sender guard (D-38/D-39)."""
    reasons: list[str] = []

    local_part = email.from_address.address.split("@", 1)[0]
    if local_part in _AUTOMATED_LOCAL_PARTS:
        reasons.append(f"local part {local_part!r} is an automated-sender pattern")

    auto_submitted = email.auto_submitted
    if auto_submitted and auto_submitted.strip().lower() != "no":
        reasons.append(f"Auto-Submitted: {auto_submitted}")

    precedence = email.header("Precedence")
    if precedence and precedence.strip().lower() in _BULK_PRECEDENCE_VALUES:
        reasons.append(f"Precedence: {precedence}")

    if email.header("List-Unsubscribe"):
        reasons.append("List-Unsubscribe header present")

    if email.from_address.address == own_address.strip().lower():
        reasons.append("sender is the mailbox's own address")

    return tuple(reasons)


def _impersonation_reason(display_name: str, from_domain: str, from_address: str) -> str | None:
    """Display-name impersonation heuristics (D-44)."""
    name = display_name.strip()
    if not name:
        return None

    email_match = _EMAIL_LIKE_RE.search(name)
    if email_match and email_match.group(0).lower() != from_address.lower():
        return f"display name contains a different address ({email_match.group(0)})"

    domain_match = _DOMAIN_LIKE_RE.search(name)
    if domain_match and domain_match.group(1).lower() != from_domain:
        return (
            f"display name contains a domain ({domain_match.group(1)}) different from the "
            f"sender's ({from_domain})"
        )

    lowered = name.lower()
    for word in _BRAND_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered) and word not in from_domain:
            return (
                f"display name mentions {word!r} but the sender's domain ({from_domain}) "
                f"is unrelated to it"
            )

    return None


def _link_host(href: str) -> str:
    """The href's host, with any userinfo and port stripped."""
    netloc = urlsplit(href).netloc
    host = netloc.rsplit("@", 1)[-1] if "@" in netloc else netloc
    return host.rsplit(":", 1)[0]


def _host_based_link_issues(href: str) -> list[LinkIssue]:
    """Credential/IP/shortener/punycode checks that only need the href (D-44)."""
    issues: list[LinkIssue] = []
    netloc = urlsplit(href).netloc
    host = _link_host(href)

    if "@" in netloc:
        issues.append(LinkIssue(href, "URL contains credentials before the host"))
    if _IPV4_RE.match(host):
        issues.append(LinkIssue(href, f"link points to an IP address ({host})"))
    if host.lower() in _SHORTENERS:
        issues.append(LinkIssue(href, f"link uses a known URL shortener ({host})"))
    if "xn--" in host.lower():
        issues.append(LinkIssue(href, f"link host is punycode-encoded ({host})"))
    return issues


def _link_issues(text_body: str) -> tuple[LinkIssue, ...]:
    """Suspicious links found in the body text (D-44).

    Links are examined as text only (host, link text); never fetched. The
    "link text looks like a domain but points elsewhere" check only applies
    to the `text (href)` shape (it needs visible link text to compare); the
    host-based checks (IP, shortener, punycode, credentials) apply to every
    http(s) URL in the body, including a bare URL with no surrounding markup.
    """
    issues: list[LinkIssue] = []
    seen_hrefs: set[str] = set()

    for match in _LINK_RE.finditer(text_body):
        href = match.group("url")
        seen_hrefs.add(href)
        issues.extend(_host_based_link_issues(href))

        # The domain-like text may be preceded by other words ("Please visit
        # paypal.com (https://evil.io)"); only its last token needs to look
        # like a domain.
        link_text = (match.group(1) or "").strip()
        last_token = link_text.rsplit(None, 1)[-1] if link_text else ""
        host = _link_host(href)
        named_domain = last_token.lower()
        lowered_host = host.lower()
        # The link text names domain D; it's not an issue when the host either *is* D
        # or is a subdomain of it (task 52) -- "paypal.com (https://email.paypal.com/x)"
        # is a legitimate subdomain, unlike "microsoft.com (http://micros0ft-billing.com)".
        is_subdomain = lowered_host.endswith(f".{named_domain}")
        if (
            last_token
            and _BARE_DOMAIN_RE.match(last_token)
            and named_domain != lowered_host
            and not is_subdomain
        ):
            issues.append(
                LinkIssue(
                    href, f"link text ({last_token}) looks like a domain but points to {host}"
                )
            )

    for match in _URL_RE.finditer(text_body):
        href = match.group(0)
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        issues.extend(_host_based_link_issues(href))

    return tuple(issues)


def _link_hosts(text_body: str) -> tuple[str, ...]:
    """Every distinct link host in `text_body`, in order of appearance (task 52).

    Covers both the `text (href)` shape and a bare http(s) URL: `_URL_RE` matches the
    href in either case, so one pass over it is enough (D-44: text only, never fetched).
    """
    hosts = (_link_host(match.group(0)).lower() for match in _URL_RE.finditer(text_body))
    return tuple(dict.fromkeys(host for host in hosts if host))


def _risky_attachments(email: Email) -> tuple[str, ...]:
    """Risky attachment types (D-44): HTML, archives, executables, macro docs."""
    risky: list[str] = []
    for attachment in email.attachments:
        _, _, extension = attachment.filename.lower().rpartition(".")
        if f".{extension}" in _RISKY_EXTENSIONS:
            risky.append(attachment.filename)
    return tuple(risky)


def _bulk_cues(email: Email) -> tuple[str, ...]:
    """Bulk-mail cues from headers and body content (D-44)."""
    cues: list[str] = []

    if email.header("List-Unsubscribe"):
        cues.append("List-Unsubscribe header present")

    precedence = email.header("Precedence")
    if precedence and precedence.strip().lower() == "bulk":
        cues.append("Precedence: bulk")

    lowered_body = email.text_body.lower()
    if "unsubscribe" in lowered_body:
        cues.append("unsubscribe footer text in the body")
    if any(phrase in lowered_body for phrase in _BULK_BODY_PHRASES[1:]):
        cues.append('"view in browser" text in the body')

    return tuple(cues)


def extract_facts(
    email: Email,
    *,
    own_address: str,
    internal_domains: Sequence[str],
    allowlist_senders: Sequence[str],
    allowlist_domains: Sequence[str],
    gmail_labels: Sequence[str],
) -> EmailFacts:
    """Extract deterministic phishing/bulk/trust facts from `email` (D-44).

    `internal_domains`, `allowlist_senders` and `allowlist_domains` are the
    matching lists (D-42, D-61). Links are analysed as text only, never
    fetched; attachments are never opened.
    """
    sender_domain = email.from_address.domain
    auth = _parse_authentication_results(email.header("Authentication-Results"))

    internal = False
    internal_reason: str | None = None
    if sender_domain in {domain.strip().lower() for domain in internal_domains}:
        # D-42: exact domain match only (no subdomain rule, unlike D-61).
        passed, auth_failure = _domain_auth_passed(auth, sender_domain)
        internal = passed
        if not passed:
            internal_reason = (
                f"domain {sender_domain} is in INTERNAL_DOMAINS but authentication failed "
                f"({auth_failure}); treated as external"
            )

    allowlisted = False
    allowlist_reason: str | None = None
    address_match = email.from_address.address in {
        sender.strip().lower() for sender in allowlist_senders
    }
    domain_match = any(
        sender_domain == listed or sender_domain.endswith(f".{listed}")
        for listed in (domain.strip().lower() for domain in allowlist_domains)
        if listed
    )
    if address_match or domain_match:
        passed, auth_failure = _domain_auth_passed(auth, sender_domain)
        allowlisted = passed
        if not passed:
            allowlist_reason = (
                f"sender matched the allowlist but authentication failed for {sender_domain} "
                f"({auth_failure}); not treated as allowlisted"
            )

    reply_to_domain = email.reply_to.domain if email.reply_to is not None else None
    reply_to_mismatch = reply_to_domain is not None and reply_to_domain != sender_domain

    return EmailFacts(
        sender_domain=sender_domain,
        auth=auth,
        internal=internal,
        internal_reason=internal_reason,
        allowlisted=allowlisted,
        allowlist_reason=allowlist_reason,
        automated_reasons=_automated_sender_reasons(email, own_address),
        reply_to_domain=reply_to_domain,
        reply_to_mismatch=reply_to_mismatch,
        impersonation_reason=_impersonation_reason(
            email.from_address.name, sender_domain, email.from_address.address
        ),
        link_issues=_link_issues(email.text_body),
        link_hosts=_link_hosts(email.text_body),
        risky_attachments=_risky_attachments(email),
        bulk_cues=_bulk_cues(email),
        gmail_category_labels=tuple(
            label for label in gmail_labels if label.startswith("CATEGORY_")
        ),
        free_mail_sender=sender_domain in _FREE_MAIL_DOMAINS,
        calendar_invitation=bool(email.calendar_parts),
    )
