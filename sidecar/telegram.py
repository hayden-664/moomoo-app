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
