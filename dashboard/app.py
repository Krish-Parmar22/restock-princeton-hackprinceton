"""Owner dashboard: stock, variance with camera proof, flagged grabs to confirm, agent trust, Excel.

Run: cd dashboard && uv run streamlit run app.py
Reads the agent API at API_URL (default http://localhost:8000). If it's down, shows demo data, clearly labelled.
"""
from __future__ import annotations

import base64
import hmac
import io
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

from client import ApiError, connect
from excel import workbook_bytes

ACTIONS = ["took", "put_back", "touched"]
QTYS = ["1", "2", "3", "pinch", "handful", "whole_container"]   # exact counts for cans; amounts for bulk food

st.set_page_config(page_title="Inventory", page_icon="🍕", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """<style>
    .card {border-radius:12px;padding:14px 16px;border:1px solid rgba(128,128,128,.25);background:rgba(128,128,128,.06)}
    .card.low {border-color:#e5484d;background:rgba(229,72,77,.10)}
    .card .name {font-size:.85rem;opacity:.75;text-transform:capitalize}
    .card .val {font-size:1.6rem;font-weight:700;line-height:1.2}
    .card .sub {font-size:.8rem;opacity:.75}
    .pill {display:inline-block;padding:1px 8px;border-radius:999px;font-size:.75rem;font-weight:600}
    .pill.low {background:#e5484d;color:#fff}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def api():
    return connect()


def load(fn: str, *args):
    """Cached API GET, keyed by call. Cleared after every confirm."""
    return _load(fn, *args)


@st.cache_data(ttl=15, show_spinner=False)
def _load(fn: str, *args):
    return getattr(api(), fn)(*args)


def label(s: str | None) -> str:
    return (s or "?").replace("_", " ")


def frame_image(path: str):
    p = Path(path)
    return str(p) if p.exists() else None


@st.cache_data(show_spinner=False, max_entries=200)
def gif(frames: dict) -> bytes | None:
    # Small (480 px wide) so it's quick to build and send; cached per grab.
    paths = [frames["before"], *frames["during"], frames["after"]]
    imgs = []
    for p in paths:
        if Path(p).exists():
            im = Image.open(p).convert("RGB")
            im.thumbnail((480, 480))
            imgs.append(im)
    if len(imgs) < 2:
        return None
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=[700] + [450] * (len(imgs) - 2) + [1200], loop=0)
    return buf.getvalue()


def show_frames(g: dict, key: str) -> None:
    f = g["frames"]
    cols = st.columns(2 + len(f["during"]))
    shots = [("Before", f["before"]), *[(f"During {i + 1}", p) for i, p in enumerate(f["during"])], ("After", f["after"])]
    for col, (name, path) in zip(cols, shots):
        img = frame_image(path)
        if img:
            col.image(img, caption=name, width="stretch")
        else:
            col.caption(f"{name}: frame not on this machine")
    # Plain HTML <details>: opening it doesn't rerun the app. (A Streamlit toggle reran the page,
    # which collapsed the expander it lives in, so the clip never showed.)
    clip = gif(f)
    if clip:
        b64 = base64.b64encode(clip).decode()
        st.markdown(f'<details><summary style="cursor:pointer">▶ Play as clip</summary>'
                    f'<img src="data:image/gif;base64,{b64}" style="max-width:480px;width:100%;border-radius:8px;margin-top:6px"></details>',
                    unsafe_allow_html=True)


def grab_caption(g: dict) -> str:
    t = datetime.fromisoformat(str(g["t_start"]).replace("Z", "+00:00")).strftime("%a %-I:%M:%S %p")
    conf = f"{g['confidence']:.0%}" if g.get("confidence") is not None else "?"
    where = "📍 Casa Piada" if str(g["grab_id"]).endswith("-shop") else "🔴 Live"
    return f"{where} · **{t}** · {label(g['zone'])} · {label(g.get('action'))} {label(g.get('qty_label'))} of {label(g.get('item'))} · confidence {conf} · `{g['status']}`"


# ---------- password (public demo link) ----------
VIEW_KEY = os.getenv("DASHBOARD_VIEW_KEY", "")
if VIEW_KEY and not st.session_state.get("viewer_ok"):
    st.title("🍕 Inventory")
    pw = st.text_input("Password", type="password")
    if pw and hmac.compare_digest(pw, VIEW_KEY):
        st.session_state["viewer_ok"] = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    st.stop()

# ---------- setup (no sidebar: the page password is the only gate) ----------
a = api()
today = date.today()
day = today
start, end = today - timedelta(days=6), today
can_edit = True

try:
    inventory = load("inventory")
except ApiError as e:
    st.error(str(e))
    st.stop()

# ---------- header + Excel ----------
h1, h2 = st.columns([3, 1])
h1.title("Today at the shop")
h1.caption(today.strftime("%A, %B %-d") + (" · ⚠ demo data (agent API offline)" if a.demo else " · 🟢 live"))
if h2.button("↻ Refresh", width="stretch"):
    _load.clear()
try:
    try:
        orders = load("orders", 50)
    except ApiError:
        orders = []
    sheet = workbook_bytes(inventory, today, load("grabs", today, "all"), orders)
    h2.download_button("⬇ Download today's sheet", sheet, file_name=f"inventory-{today}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       type="primary", width="stretch")
except ApiError as e:
    h2.error(f"Sheet unavailable: {e}")

# ---------- stock cards ----------
st.subheader("Stock")
low_first = sorted(inventory, key=lambda r: (not r["below_par"], r["item"]))
for chunk in [low_first[i:i + 4] for i in range(0, len(low_first), 4)]:
    for col, r in zip(st.columns(4), chunk):
        low = r["below_par"]
        col.markdown(
            f"""<div class="card {'low' if low else ''}">
              <div class="name">{label(r['item'])} {'<span class="pill low">below par</span>' if low else ''}</div>
              <div class="val">{r['on_hand']:g} {r['unit']}</div>
              <div class="sub">par {r['par']:g} · used today {r['used_today']:g} · ${r['unit_cost']:.2f}/{r['unit']}</div>
            </div>""",
            unsafe_allow_html=True,
        )

# ---------- reorder plan + orders ----------
st.subheader("Reorder")
try:
    plan = load("reorder_plan")
except ApiError as e:
    plan = {"items": [], "total_cost": 0}
    st.error(str(e))
p1, p2 = st.columns([3, 1])
if plan["items"]:
    p1.markdown(f"**Below par now: {len(plan['items'])} item(s), ${plan['total_cost']:,.2f} to restock** "
                "(each back up to 2× par)")
    p1.dataframe(pd.DataFrame([{"Item": label(i["item"]).title(), "On hand": f"{i['on_hand']:g} {i['unit']}",
                                "Par": i["par"], "Order": i["order_qty"], "Cost": i["cost"]} for i in plan["items"]]),
                 hide_index=True, width="stretch", column_config={"Cost": st.column_config.NumberColumn(format="$%.2f")})
else:
    p1.caption("Everything is at or above par. Nothing to order.")
if p2.button("📱 Text me the morning debrief", type="primary", width="stretch",
             help="How yesterday went, grabs to confirm, and today's order: one text, approve with yes."):
    try:
        a.send_briefing()
        st.toast("Debrief on its way (about a minute). Reply yes on your phone to order.")
    except ApiError as e:
        st.error(str(e))
if p2.button("📱 Text me this plan", width="stretch", disabled=not plan["items"],
             help="Just the reorder plan; reply yes on your phone to order."):
    try:
        out = a.send_plan()
        st.toast("Sent to your phone. Reply yes there to order." if out else "No owner phone has texted the line yet.")
    except ApiError as e:
        st.error(str(e))
try:
    orders_list = load("orders", 30)
except ApiError:
    orders_list = []
if orders_list:
    with st.expander(f"Orders ({len(orders_list)})", expanded=False):
        st.dataframe(pd.DataFrame([{
            "Placed": datetime.fromisoformat(o["ts"]).strftime("%a %-I:%M %p"), "Item": label(o["item"]).title(),
            "Qty": f"{o['qty']:g} {o['unit']}", "Cost": o["cost"], "Status": o["status"],
        } for o in orders_list]), hide_index=True, width="stretch",
            column_config={"Cost": st.column_config.NumberColumn(format="$%.2f")})

# ---------- needs your OK ----------
try:
    flagged = load("grabs", day, "flagged")
except ApiError as e:
    flagged = []
    st.error(str(e))
st.subheader(f"Needs your OK ({len(flagged)})")
if not flagged:
    st.caption(f"Nothing to check for {day:%b %-d}. The camera was sure about every grab.")
items = sorted({r["item"] for r in inventory})
def _confirm(g, action, item, qty, msg):
    try:
        a.confirm(g["grab_id"], action, item, qty)
        _load.clear()
        st.toast(msg)
        st.rerun()
    except ApiError as e:
        st.error(str(e))


for g in flagged:
    with st.expander(grab_caption(g), expanded=len(flagged) <= 3):
        show_frames(g, f"flag-{g['grab_id']}")
        units = {r["item"]: r["unit"] for r in inventory}
        it = g.get("item") or g["zone"]
        counted = units.get(it) == "each"
        default_qty = g.get("qty_label") if g.get("qty_label") in QTYS else ("1" if counted else "handful")
        saw = g.get("action") in ACTIONS
        b1, b2, b3, _ = st.columns([1, 1, 1, 1])
        if b1.button("✓ Right", key=f"ok-{g['grab_id']}", type="primary", disabled=not saw,
                     help=f"Count it as the camera said: {label(g.get('action'))} {label(default_qty)} {label(it)}"):
            _confirm(g, g["action"], it, default_qty, f"Counted: {label(g['action'])} {label(default_qty)} {label(it)}")
        if counted and b2.button("✓ Took 1", key=f"one-{g['grab_id']}",
                                 help=f"One {label(it)} was taken."):
            _confirm(g, "took", it, "1", f"Counted: took 1 {label(it)}")
        if b3.button("✗ Nothing taken", key=f"no-{g['grab_id']}",
                     help="Just a touch or a false alarm: no stock change."):
            _confirm(g, "touched", it, default_qty, "Marked as nothing taken")
        with st.popover("Fix it"):
            with st.form(f"confirm-{g['grab_id']}"):
                action = st.selectbox("What happened", ACTIONS, index=ACTIONS.index(g["action"]) if g.get("action") in ACTIONS else 0, format_func=label)
                item_opts = sorted(set(items) | {g.get("item") or g["zone"]})
                item = st.selectbox("Item", item_opts, index=item_opts.index(g.get("item") or g["zone"]), format_func=label)
                qty = st.selectbox("How much", QTYS, index=QTYS.index(g["qty_label"]) if g.get("qty_label") in QTYS else 0, format_func=label)
                if st.form_submit_button("Save", type="primary"):
                    _confirm(g, action, item, qty, f"Saved: {label(action)} {label(qty)} of {label(item)}")

# ---------- variance ----------
st.subheader("Where the food went")
st.caption(f"Should have used (register sales × recipe) vs actually used, {start:%b %-d} – {end:%b %-d}. Biggest money gaps first. Click a row for the camera proof.")
try:
    variance = sorted(load("variance", start, end), key=lambda r: abs(r["variance_usd"]), reverse=True)
except ApiError as e:
    variance = []
    st.error(str(e))

if variance:
    df = pd.DataFrame([{
        "Item": label(r["item"]), "Should use": r["theoretical"], "Used": r["actual"], "Unit": r["unit"],
        "Gap": r["variance_qty"], "Gap $": r["variance_usd"], "Camera events": len(r["grab_ids"]),
    } for r in variance])
    total = sum(r["variance_usd"] for r in variance if r["variance_usd"] > 0)
    st.metric("Unexplained cost", f"${total:,.2f}", help="Sum of positive $ gaps in the date range")
    sel = st.dataframe(
        df.style.format({"Should use": "{:g}", "Used": "{:g}", "Gap": "{:+g}", "Gap $": "${:+,.2f}"})
        .map(lambda v: "color:#e5484d;font-weight:600" if isinstance(v, (int, float)) and v > 0 else "", subset=["Gap", "Gap $"]),
        hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="variance",
    )
    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        r = variance[rows[0]]
        st.markdown(f"#### {label(r['item']).title()}: {len(r['grab_ids'])} camera events")
        by_day: dict[date, set[str]] = defaultdict(set)
        for gid in r["grab_ids"]:
            try:
                by_day[date.fromisoformat(gid[:10])].add(gid)
            except ValueError:
                pass
        grabs = []
        for d, ids in sorted(by_day.items()):
            try:
                grabs += [g for g in load("grabs", d, "all") if g["grab_id"] in ids]
            except ApiError as e:
                st.error(str(e))
        if not grabs:
            st.caption("No camera events for this item in the range.")
        for g in sorted(grabs, key=lambda g: g["t_start"], reverse=True)[:20]:
            st.markdown(grab_caption(g))
            show_frames(g, f"var-{g['grab_id']}")
            st.divider()
else:
    st.caption("No variance data for these dates.")

# ---------- behind the scenes (for judges): the iMessage thread + the deepagents run ----------
try:
    msgs = load("messages", 60)
except ApiError:
    msgs = []
try:
    trace = load("trace")
except ApiError:
    trace = {"steps": []}
if msgs or trace.get("steps"):
    with st.expander("Behind the scenes (for judges): the iMessage thread and how the agent decided", expanded=False):
        st.subheader("📱 iMessage with the owner (Photon)")
        st.caption("Both sides of the real thread. Every agent text is fact-checked by d1 before it's sent; low scores are softened or blocked.")
        if msgs:
            out = [m for m in msgs if m["decision"] != "received"]
            counts = pd.Series([m["decision"] for m in out]).value_counts() if out else pd.Series(dtype=int)
            m1, m2, m3 = st.columns(3)
            m1.metric("Sent", int(counts.get("sent", 0)))
            m2.metric("Softened", int(counts.get("softened", 0)))
            m3.metric("Blocked by d1", int(counts.get("blocked", 0)))
            for m in reversed(msgs[:30]):   # oldest first, like a phone
                when = datetime.fromisoformat(str(m["ts"]).replace("Z", "+00:00")).strftime("%-I:%M %p")
                if m["decision"] == "received":
                    with st.chat_message("user", avatar="🧑‍🍳"):
                        st.markdown(f"{m['text']}  \n<span style='opacity:.5;font-size:.75rem'>{when}</span>", unsafe_allow_html=True)
                else:
                    tag = {"sent": "", "softened": " · 🟡 softened by d1", "blocked": " · 🔴 blocked by d1 (never sent)"}.get(m["decision"], "")
                    score = f" · d1 {m['d1_score']:.2f}" if m.get("d1_score") is not None else ""
                    with st.chat_message("assistant", avatar="🍕"):
                        body = m["text"] if m["decision"] != "blocked" else f"~~{m['text']}~~"
                        st.markdown(f"{body}  \n<span style='opacity:.5;font-size:.75rem'>{when}{score}{tag}</span>",
                                    unsafe_allow_html=True)


        st.subheader("🧠 Under the hood: how the agent decided")
        st.caption("The agent's latest run, read live from its LangGraph state: LangChain deepagents delegating to subagents, "
                   "calling tools, saving files, the d1 fact-check on every text, and pausing for the owner's approval.")
        ICON = {"trigger": "📨", "parallel": "⚡", "subagent": "🤖", "subagent_result": "📋", "tool": "🔧", "file": "📁",
                "plan": "🗒️", "draft": "✍️", "guard": "🛡️", "approval": "✋", "paused": "⏸️", "reply": "💬"}
        if trace.get("steps"):
            for s_ in trace["steps"]:
                k = s_["kind"]
                head = {"trigger": "Trigger", "parallel": "Delegation", "subagent": f"Subagent **{s_.get('name')}**",
                        "subagent_result": f"**{s_.get('name')}** reported", "tool": f"Tool `{s_.get('name')}`",
                        "file": f"File `{s_.get('name')}`", "plan": "Plan (todos)", "draft": "Draft text",
                        "guard": "d1 guard", "approval": "Reorder needs approval", "paused": "Human in the loop",
                        "reply": "Reply"}.get(k, k)
                if k == "guard" and not s_.get("ok", True):
                    head = "d1 guard **blocked** a claim; the agent rewrote it"
                if k in ("subagent_result", "draft"):
                    with st.expander(f"{ICON.get(k, '•')} {head}", expanded=k == "draft"):
                        st.markdown(s_["text"])
                else:
                    st.markdown(f"{ICON.get(k, '•')} {head}: {s_['text']}")
