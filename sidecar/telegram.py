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
    not re-interpreted, ``**``/``__`` are consumed before single ``*``/``_`` so
    bold does not get parsed as two italics, and the horizontal-rule check runs
    before either so a bare ``_____`` is dropped rather than read as bold.
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
    # A markdown rule renders as three-plus stray dashes/asterisks/underscores;
    # drop the line entirely. Must run before the emphasis regexes below, or a
    # bare run of underscores reads as an (empty-ish) bold span instead.
    text = re.sub(r"(?m)^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+?)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "• ", text)

    for i, content in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", content)
    return text


def _strip_md(text: str) -> str:
    """Last-resort plain rendering: drop the markers rather than show them."""
    text = re.sub(r"```[a-zA-Z0-9_-]*\n(.*?)```", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+?)_(?![\w_])", r"\1", text)
    return re.sub(r"(?m)^\s{0,3}[-*+]\s+", "• ", text)


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


def _trace_line(leaf: dict) -> str:
    """One condition, as it was actually evaluated."""
    mark = "✓" if leaf.get("result") else "✗"
    target = leaf.get("target")
    target = "" if target is None else f" {target}"
    return f"{mark} {leaf.get('path')} = {leaf.get('resolved')} ({leaf.get('op')}{target})"


def format_paper_order(order: dict, rule_id: str, trace: list[dict] | None = None) -> str:
    """Receipt for a simulated order.

    Says SIMULATED on every message. The whole value of this engine is that the
    numbers are at real scale while the fills are not, and a receipt that reads
    like a real one would erode exactly that distinction.
    """
    side = order.get("side", "?")
    arrow = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    code = order.get("code", "?")
    qty = order.get("qty")
    limit = order.get("limit_price")

    lines = [
        f"{arrow} <b>SIMULATED {side} {qty} {code}</b> @ {money(limit or 0)} limit",
        f"<i>rule: {rule_id}</i>",
    ]
    if order.get("stop_price"):
        lines.append(
            f"Stop reference: {money(order['stop_price'])} "
            f"<i>(ATR {order.get('atr14')}, risk {money(order.get('risk_usd') or 0)})</i>"
        )
    if order.get("est_fee_round_trip"):
        # Estimated, and said so every time: the paper account is charged
        # nothing, so a receipt that quietly implied a real cost would be the
        # one number on here that is not a measurement.
        lines.append(
            f"Est. round-trip cost: {money(order['est_fee_round_trip'])} "
            f"({order.get('est_fee_pct')}% of the position)"
        )
    if order.get("breakeven_price"):
        lines.append(
            f"Breakeven after costs: {money(order['breakeven_price'])} "
            f"<i>(estimated)</i>"
        )
    if order.get("scaled"):
        lines.append(
            f"⚠️ <i>Scaled to {qty} from {order.get('qty_intended')} — "
            f"paper buying power, not the sizing rule</i>"
        )
    if order.get("order_id"):
        lines.append(f"Order {order['order_id']} · {order.get('status', '?')}")

    if trace:
        lines.append("")
        lines.append("<i>Conditions:</i>")
        lines.extend(f"<code>{_trace_line(t)}</code>" for t in trace)

    lines.append("")
    lines.append(
        "<i>Paper account. No real money moved, and no fees were charged — "
        "the cost above is an estimate from the real account's own schedule.</i>"
    )
    return "\n".join(lines)


def format_paper_fill(fill: dict) -> str:
    side = fill.get("trd_side", "?")
    arrow = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    qty, price = fill.get("qty"), fill.get("price")
    notional = (qty or 0) * (price or 0)
    return "\n".join(
        [
            f"{arrow} <b>SIMULATED FILL: {side} {qty} {fill.get('code')}</b> @ {money(price or 0)}",
            f"Notional {money(notional)} · order {fill.get('order_id')}",
            "<i>Paper account. No real money moved.</i>",
        ]
    )


def _num(value) -> float | None:
    """moomoo returns the string "N/A" where a number is expected."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def format_paper_session(summary: dict, extra: dict | None = None) -> str:
    """The simulated book's own daily note.

    Deliberately a separate message from the daily review rather than a section
    inside it. The review reports a real brokerage account; this reports money
    that does not exist. Merging them would put a fabricated P&L one line from a
    real one under a single heading - the same provenance rule `pnl.py` follows
    when it refuses to present `closed_realized` as broker truth, except that
    two messages cannot be misread at a glance the way one label can.
    """
    extra = extra or {}
    entries = summary.get("entries") or []
    exits = summary.get("exits") or []
    lines = [
        "\U0001f9ea <b>Paper engine \u2014 simulated session</b>",
        f"<i>{summary.get('session')} \u00b7 fake money, simulated account 5367857</i>",
        "",
    ]

    # The first thing worth knowing is whether it ran at all.
    if not summary.get("entries_evaluated"):
        lines.append(
            "\u26a0\ufe0f <b>Entries were never evaluated.</b> The window passed "
            "with no cycle in it \u2014 nothing new could be bought."
        )
    else:
        lines.append(f"Entry window ran. {len(entries)} entry(s), {len(exits)} exit(s).")

    if entries or exits:
        lines.append("")
        for o in entries + exits:
            filled = _num(o.get("dealt_qty")) or 0
            px = _num(o.get("dealt_avg_price")) or _num(o.get("price"))
            lines.append(
                f"\u2022 {o.get('trd_side')} {html.escape(str(o.get('code')))} "
                f"\u00d7{filled:g}"
                + (f" @ {px:,.2f}" if px else "")
                + f" \u2014 {o.get('status')}"
            )
        fee = summary.get("est_fees_round_trip") or 0
        if fee:
            lines.append(
                f"<i>Est. round-trip cost {fee:,.2f} \u2014 modelled from the real "
                f"account's fee history; the paper account is charged nothing.</i>"
            )

    blocked = summary.get("blocked") or {}
    if blocked:
        lines.append("")
        lines.append("<i>Declined:</i> " + ", ".join(f"{r} \u00d7{n}" for r, n in blocked.items()))

    nm = (summary.get("outcomes") or {}).get("NO_MATCH", 0)
    lines.append("")
    lines.append(
        f"<i>{summary.get('cycles', 0)} cycle(s), {nm} condition(s) tested and not met.</i>"
    )

    equity = _num(extra.get("paper_equity"))
    if equity is not None:
        change = _num(extra.get("paper_change"))
        lines.append(
            f"<i>Simulated equity {equity:,.2f}"
            + (f" ({change:+,.2f} on the session)" if change is not None else "")
            + ".</i>"
        )
    if extra.get("halted"):
        lines.append("")
        lines.append(f"\U0001f6d1 <b>Engine halted:</b> {extra.get('kill_reason')}")

    lines.append("")
    lines.append(
        "<i>No real money moved. Your real account is reported separately in the "
        "daily review.</i>"
    )
    return "\n".join(lines)


def format_paper_digest(status: dict) -> str:
    """Daily note naming what the engine could not consider, and why.

    Excluded symbols are named rather than dropped, for the same reason
    unconvertible currencies are named in the P&L: a name that vanishes without
    explanation looks identical to one that simply did not qualify.
    """
    universe = status.get("universe") or {}
    excluded = universe.get("excluded") or []
    lines = [
        "\U0001f4dd <b>Paper engine — daily note</b>",
        f"Considering {len(universe.get('considered') or [])} symbol(s).",
    ]
    if excluded:
        lines.append("")
        lines.append(f"<i>Excluded ({len(excluded)}):</i>")
        for item in excluded:
            lines.append(f"• {item['code']} — {item['reason']}: {item.get('detail','')}")
    if status.get("killed"):
        lines.append("")
        lines.append(f"⚠️ <b>Engine halted:</b> {status.get('kill_reason')}")
    lines.append("")
    lines.append("<i>Paper account. No real money moved.</i>")
    return "\n".join(lines)
