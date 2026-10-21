"""The emails about signing in: one-time codes and the notice of a new way to sign in.

Each function returns ``(subject, text, html)``. Anyone can make the Platform send these
to any address, so they carry no text a user typed: the greeting is the address itself.
A code is in the body only, never in the subject, which shows in notifications.
"""

from datetime import datetime
from html import escape

from management.infra.email_templates import PRODUCT, _Message, _render, local_time

DO_NOT_SHARE = f"Do not share this code with anyone. The {PRODUCT} never asks for it."
DO_NOT_SHARE_ANY = DO_NOT_SHARE.replace("this code", "this code or link")


def _until(expires_at: datetime, minutes: int) -> str:
    return f"{local_time(expires_at)}, in {minutes} minutes"


def sign_in_code(
    email: str, code: str, link: str | None, expires_at: datetime, minutes: int
) -> tuple[str, str, str]:
    """A short, centred message: a heading, a sign-in button, then the code to type.

    It does not use the shared layout: the reader wants one action, so the button and
    the code are the whole message.
    """
    until = _until(expires_at, minutes)
    reason = (
        f"You received this email because someone asked to sign in as {email}. "
        "If that was not you, ignore this email: nobody can sign in without it."
    )

    lines = [f"Sign in to the {PRODUCT}", "", f"Hello {email},", ""]
    if link:
        lines += ["Open this link to sign in:", f"Sign-in link: {link}", "", "Or enter this code:"]
    else:
        lines += ["Enter this code on the page that asked for it:"]
    lines += [f"Sign-in code: {code}", "", f"Valid until: {until}", "", DO_NOT_SHARE_ANY]
    lines += ["", "--", reason, PRODUCT]
    text = "\n".join(lines) + "\n"

    muted = "color:#6b6b6b;font-size:13px;line-height:1.5"
    button = ""
    if link:
        href = escape(link, quote=True)
        button = (
            '<tr><td align="center" style="padding:8px 0 28px">'
            f'<a href="{href}" style="display:inline-block;background:#1f1f1f;color:#ffffff;'
            "text-decoration:none;font-weight:600;font-size:15px;padding:12px 28px;"
            f'border-radius:8px">Sign in to the {PRODUCT}</a></td></tr>'
            f'<tr><td align="center" style="{muted};padding-bottom:12px">'
            "Or enter this code</td></tr>"
        )
    else:
        button = (
            f'<tr><td align="center" style="{muted};padding-bottom:12px">'
            "Enter this code on the page that asked for it</td></tr>"
        )
    html = (
        '<div style="background:#f5f4ef;padding:40px 16px;'
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;"
        'color:#1f1f1f">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e6e3da;'
        'border-radius:12px">'
        '<tr><td style="padding:40px 40px 32px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'<tr><td align="center" style="font-size:14px;font-weight:600;letter-spacing:.02em;'
        f'color:#6b6b6b;padding-bottom:24px">{PRODUCT}</td></tr>'
        '<tr><td align="center" style="font-size:24px;font-weight:600;line-height:1.3;'
        f'padding-bottom:28px">Sign in to the {PRODUCT}</td></tr>'
        f"{button}"
        '<tr><td align="center" style="padding-bottom:12px">'
        '<span style="display:inline-block;background:#f5f4ef;border-radius:8px;'
        "padding:14px 24px;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:28px;"
        f'font-weight:600;letter-spacing:6px">{escape(code)}</span></td></tr>'
        f'<tr><td align="center" style="{muted};padding-bottom:28px">'
        f"Valid until {escape(until)}</td></tr>"
        f'<tr><td style="{muted};border-top:1px solid #e6e3da;padding-top:20px">'
        f"{escape(DO_NOT_SHARE_ANY)}<br><br>{escape(reason)}</td></tr>"
        "</table></td></tr></table>"
        f'<p style="{muted};text-align:center;margin:20px 0 0">{PRODUCT}</p>'
        "</div>"
    )
    return f"Your {PRODUCT} sign-in code", text, html


def google_linked(email: str, linked_at: datetime) -> tuple[str, str, str]:
    text, html = _render(
        _Message(
            greeting_name=email,
            intro=f"A Google account can now sign in to your {PRODUCT} account.",
            details=[("Account", email), ("Linked at", local_time(linked_at))],
            steps=[
                "If you signed in with Google yourself, you do not need to do anything.",
                "If you did not, tell an administrator.",
            ],
            reason=(
                f"You received this email because someone signed in to {email} with Google "
                "for the first time."
            ),
        )
    )
    return f"Google sign-in was added to your {PRODUCT} account", text, html
