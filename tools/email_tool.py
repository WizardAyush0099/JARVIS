"""Email sending over SMTP.

Requirements this satisfies:

* recipient / subject / body / optional attachment
* confirmation before sending (the router asks; ``EMAIL_AUTO_SEND=true`` opts out)
* credentials come from the environment only - never from source, never logged
* the return value reflects what actually happened.  "I sent it" is only ever
  said after the SMTP server accepted the message.
"""

from __future__ import annotations

import mimetypes
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any, List, Optional

from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.email")

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def _settings(ctx: Optional[ToolContext]) -> Any:
    return getattr(ctx, "settings", None) if ctx is not None else None


def _split_recipients(raw: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,;]", raw or "") if part.strip()]


@tool(
    name="send_email",
    description="Send an email (asks for confirmation first).",
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "recipient address (comma separated for several)"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "attachment": {"type": "string", "description": "optional file path"},
        },
        "required": ["to", "body"],
    },
    category="email",
    dangerous=True,
    aliases=("compose_email", "email", "write_email"),
)
def send_email(
    to: str,
    body: str,
    subject: str = "",
    attachment: str = "",
    ctx: Optional[ToolContext] = None,
) -> ToolResult:
    settings = _settings(ctx)
    if settings is None:
        return ToolResult.failure("email needs a configured context")

    recipients = _split_recipients(to)
    if not recipients:
        return ToolResult.failure("who should I send it to? I need an email address.")
    invalid = [address for address in recipients if not _EMAIL_RE.match(address)]
    if invalid:
        return ToolResult.failure(
            f"'{', '.join(invalid)}' doesn't look like an email address. "
            "I need the full address, e.g. name@example.com"
        )

    email = getattr(settings, "email", None)
    if email is None or not email.configured:
        missing = []
        if not getattr(email, "enabled", False):
            missing.append("EMAIL_ENABLED=true")
        if not getattr(email, "user", ""):
            missing.append("SMTP_USER")
        if not getattr(email, "password", ""):
            missing.append("SMTP_PASSWORD")
        return ToolResult.failure(
            "email is not configured yet - set " + ", ".join(missing) + " in your .env file"
        )

    if not body or not str(body).strip():
        return ToolResult.failure("the email body is empty")

    message = EmailMessage()
    message["From"] = email.address
    message["To"] = ", ".join(recipients)
    message["Subject"] = (subject or "Message from JARVIS").strip()
    message.set_content(str(body))

    if attachment:
        try:
            from tools.files import resolve_in_sandbox

            path = resolve_in_sandbox(attachment, ctx)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"could not attach '{attachment}': {exc}")
        if not path.is_file():
            return ToolResult.failure(f"there is no file to attach at {path}")
        try:
            content_type, _ = mimetypes.guess_type(str(path))
            maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
            message.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
            )
        except OSError as exc:
            return ToolResult.failure(f"could not read the attachment: {exc}")

    timeout = 25.0
    try:
        if email.use_tls and int(email.port) == 465:
            client = smtplib.SMTP_SSL(email.host, int(email.port), timeout=timeout, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(email.host, int(email.port), timeout=timeout)
        try:
            client.ehlo()
            if email.use_tls and int(email.port) != 465:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(email.user, email.password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        return ToolResult.failure(
            "the mail server rejected the login. For Gmail you must use an App Password, not your normal password."
        )
    except smtplib.SMTPException as exc:
        return ToolResult.failure(f"the mail server refused the message: {exc}")
    except OSError as exc:
        return ToolResult.failure(f"could not reach {email.host}:{email.port} ({exc})")
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"could not send the email: {exc}")

    log.info("email sent to %d recipient(s)", len(recipients))
    return ToolResult.success(
        f"Sent the email to {', '.join(recipients)}.",
        data={"to": recipients, "subject": message["Subject"], "sent_at": time.time()},
    )


__all__ = ["send_email"]
