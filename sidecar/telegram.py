"""Telegram delivery.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment. Both absent
is a supported state: notify() becomes a no-op and reports why, so the rest of
the app runs fine before the bot is set up.
"""

from __future__ import annotations

import html
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


async def notify(text: str, *, silent: bool = False) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"}

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(API.format(token=token), json=payload)
    if r.status_code != 200:
        log.warning("telegram send failed: %s %s", r.status_code, r.text)
        return {"sent": False, "reason": f"HTTP {r.status_code}", "body": r.text}
    return {"sent": True}


# Telegram rejects any message over 4096 characters with a 400. An agent-written
# report routinely runs longer, so arbitrary text is split rather than truncated
# — a silently dropped tail is worse than a second message.
MAX_MESSAGE = 4096


def _split(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Break *text* into sendable chunks, preferring paragraph then line breaks."""
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[:limit]
        # Break at the latest paragraph boundary, else line, else hard-cut.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _md_to_html(text: str) -> str:
    """Render agent-written markdown as the small HTML subset Telegram accepts.

    Telegram supports only <b> <i> <code> <pre> <a> — not markdown. Sending raw
    markdown as plain text renders ``**bold**`` with the asterisks visible,
    which is what made the first delivered report hard to read.

    Order matters: code spans are stashed before escaping so their contents are
    not re-interpreted, and ``**`` is consumed before single ``*`` so bold does
    not get parsed as two italics.
    """
    stash: list[str] = []

    def keep(content: str) -> str:
        stash.append(content)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(
        r"```[a-zA-Z0-9_-]*\n(.*?)```",
        lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"),
        text,
        flags=re.S,
    )
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"),
        text,
    )

    text = html.escape(text)

    # Headings carry no size in Telegram, so they become bold lines.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "• ", text)
    # A markdown rule renders as three stray dashes; drop the line entirely.
    text = re.sub(r"(?m)^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", "", text)

    for i, content in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", content)
    return text


def _strip_md(text: str) -> str:
    """Last-resort plain rendering: drop the markers rather than show them."""
    text = re.sub(r"```[a-zA-Z0-9_-]*\n(.*?)```", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "• ", text)
    return re.sub(r"(?m)^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", "", text)


async def notify_text(text: str, *, silent: bool = False) -> dict:
    """Send agent-written markdown, rendered into Telegram's HTML subset.

    Delivery is never allowed to fail on formatting: if Telegram rejects the
    converted HTML, the same chunk is retried as plain text with the markers
    stripped. A readable message is preferred, but an ugly message beats none.

    Chunking happens on the markdown, before conversion, so a split can never
    land inside a generated tag.
    """
    if not text or not text.strip():
        return {"sent": False, "reason": "empty message"}

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"}

    # Split the markdown, then convert each chunk independently — converting
    # first and splitting after could cut a <b> in half and 400 the message.
    parts = _split(text)
    fell_back = False

    async with httpx.AsyncClient(timeout=15) as http:
        for i, part in enumerate(parts):
            base = {
                "chat_id": chat_id,
                "disable_web_page_preview": True,
                # Only the first chunk buzzes the phone.
                "disable_notification": silent or i > 0,
            }
            r = await http.post(
                API.format(token=token),
                json={**base, "text": _md_to_html(part), "parse_mode": "HTML"},
            )
            if r.status_code == 400:
                # Malformed HTML from an unusual construct. Send it plain
                # rather than losing the chunk.
                log.warning("telegram rejected HTML, retrying plain: %s", r.text[:200])
                fell_back = True
                r = await http.post(
                    API.format(token=token), json={**base, "text": _strip_md(part)}
                )
            if r.status_code != 200:
                log.warning("telegram send failed: %s %s", r.status_code, r.text)
                return {
                    "sent": False,
                    "reason": f"HTTP {r.status_code}",
                    "body": r.text[:300],
                    "sent_parts": i,
                }
    return {"sent": True, "parts": len(parts), "plain_fallback": fell_back}


# Enough to keep a MYR figure from reading as dollars. Anything not listed
# falls back to the ISO code, which is unambiguous if less pretty.
_SYMBOLS = {"USD": "$", "MYR": "RM", "HKD": "HK$", "SGD": "S$", "CNH": "\u00a5", "JPY": "\u00a5"}


def money(value: float, currency: str = "USD") -> str:
    sym = _SYMBOLS.get(currency)
    return f"{sym}{value:,.2f}" if sym else f"{currency} {value:,.2f}"


def format_pnl(summary: dict, account: dict | None = None) -> str:
    """Render the net P&L block as a Telegram HTML message.

    Every figure is one total in the reporting currency, with foreign holdings
    converted first. The rate is stated so the number is reproducible, and a
    currency that could not be converted is named rather than dropped quietly.
    """
    base = summary.get("base", "USD")
    conv = summary.get("conversion") or {}
    o = summary["open"]
    arrow = "\U0001f7e2" if summary["net"] >= 0 else "\U0001f534"

    lines = [
        f"{arrow} <b>Net P&amp;L: {money(summary['net'], base)}</b>",
        "",
        f"Today: {money(o['today'], base)}",
        f"Open unrealized: {money(o['open_unrealized'], base)}",
        # The window matters: anything sold before it is not in this figure.
        f"Realized: {money(summary['total_realized'], base)} "
        f"<i>(approx, since {summary['window']['start']})</i>",
        "",
        f"Market value: {money(o['market_value'], base)}",
    ]
    if account and account.get("total_assets"):
        lines.append(f"Total assets: {money(float(account['total_assets']), base)}")

    rates = conv.get("rates") or {}
    for ccy in conv.get("converted_from") or []:
        rate = rates.get(ccy)
        if rate:
            lines.append(f"<i>{ccy} converted at 1 {base} = {1 / rate:,.4f} {ccy}</i>")
    if conv.get("unconverted"):
        lines.append(
            f"\u26a0\ufe0f <i>{', '.join(conv['unconverted'])} excluded \u2014 no rate available</i>"
        )
    return "\n".join(lines)


def format_screen(results: list[dict], limit_per_code: int = 3) -> str:
    """Render screener output as a Telegram digest.

    Deliberately factual: each line states what the contract is, what it costs
    and what move it needs to break even. No ranking by desirability and no
    buy/avoid language - this reports mechanics, it does not advise.
    """
    blocks: list[str] = ["🔎 <b>Options screen</b>"]
    for r in results:
        code = r.get("underlying", "?")
        spot = r.get("spot")
        cands = r.get("candidates") or []
        if r.get("error"):
            blocks.append(f"\n<b>{code}</b>\n<i>{r['error']}</i>")
            continue
        head = f"\n<b>{code}</b>"
        if spot:
            head += f" <i>spot ${spot:,.2f}</i>"
        if not cands:
            blocks.append(head + "\n<i>nothing matched the filters</i>")
            continue
        lines = [f"• {c['characterisation']}" for c in cands[:limit_per_code]]
        blocks.append(head + "\n" + "\n".join(lines))
    blocks.append(
        "\n<i>Screening output — contract mechanics only, not advice.</i>"
    )
    return "\n".join(blocks)
