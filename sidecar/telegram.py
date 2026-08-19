"""Telegram delivery.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment. Both absent
is a supported state: notify() becomes a no-op and reports why, so the rest of
the app runs fine before the bot is set up.
"""

from __future__ import annotations

import logging
import os

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


def format_pnl(summary: dict, account: dict | None = None) -> str:
    """Render the net P&L block as a Telegram HTML message."""
    net = summary["net"]
    o = summary["open"]
    arrow = "🟢" if net >= 0 else "🔴"
    lines = [
        f"{arrow} <b>Net P&amp;L: ${net:,.2f}</b>",
        "",
        f"Today: ${o['today']:,.2f}",
        f"Open unrealized: ${o['open_unrealized']:,.2f}",
        # The window matters: anything sold before it is not in this figure.
        f"Realized: ${summary['total_realized']:,.2f} "
        f"<i>(approx, since {summary['window']['start']})</i>",
        "",
        f"Market value: ${o['market_value']:,.2f}",
    ]
    if account:
        total = account.get("total_assets")
        if total:
            lines.append(f"Total assets: ${float(total):,.2f}")
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
