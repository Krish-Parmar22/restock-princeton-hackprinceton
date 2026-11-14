"""Owner dashboard: stock, variance with camera proof, flagged grabs to confirm, agent trust, Excel.

Run: cd dashboard && uv run streamlit run app.py
Reads the agent API at API_URL (default http://localhost:8000). If it's down, shows demo data, clearly labelled.
"""
from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

from client import ApiError, connect
from excel import workbook_bytes

ACTIONS = ["took", "put_back", "touched"]
QTYS = ["pinch", "handful", "whole_container"]

st.set_page_config(page_title="Inventory", page_icon="🍕", layout="wide")
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


def gif(frames: dict) -> bytes | None:
    paths = [frames["before"], *frames["during"], frames["after"]]
    imgs = [Image.open(p).convert("RGB") for p in paths if Path(p).exists()]
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
    if st.toggle("▶ Play as clip", key=f"play-{key}"):
        clip = gif(f)
        if clip:
            st.image(clip, width=420)


def grab_caption(g: dict) -> str:
    t = datetime.fromisoformat(str(g["t_start"]).replace("Z", "+00:00")).strftime("%a %-I:%M:%S %p")
    conf = f"{g['confidence']:.0%}" if g.get("confidence") is not None else "?"
    return f"**{t}** · {label(g['zone'])} bin · {label(g.get('action'))} {label(g.get('qty_label'))} of {label(g.get('item'))} · confidence {conf} · `{g['status']}`"


# ---------- sidebar ----------
a = api()
today = date.today()
with st.sidebar:
    st.header("🍕 Inventory")
    if a.demo:
        st.warning("Demo data: the agent API isn't reachable. Start it with `uv run uvicorn app.api.server:app --port 8000`.")
    else:
        st.success("Live: connected to the agent API")
    rng = st.date_input("Variance dates", (today - timedelta(days=6), today), max_value=today)
    start, end = (rng if isinstance(rng, tuple) and len(rng) == 2 else (rng[0], rng[0]) if isinstance(rng, tuple) else (rng, rng))
    day = st.date_input("Camera day (flagged grabs)", today, max_value=today)
    if st.button("↻ Refresh"):
        _load.clear()

try:
    inventory = load("inventory")
except ApiError as e:
    st.error(str(e))
    st.stop()

# ---------- header + Excel ----------
h1, h2 = st.columns([3, 1])
h1.title("Today at the shop")
h1.caption(today.strftime("%A, %B %-d"))
try:
    sheet = workbook_bytes(inventory, today, load("grabs", today, "all"))
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
for g in flagged:
    with st.expander(grab_caption(g), expanded=len(flagged) <= 2):
        show_frames(g, f"flag-{g['grab_id']}")
        with st.form(f"confirm-{g['grab_id']}"):
            c1, c2, c3 = st.columns(3)
            action = c1.selectbox("What happened", ACTIONS, index=ACTIONS.index(g["action"]) if g.get("action") in ACTIONS else 0, format_func=label)
            item_opts = sorted(set(items) | {g.get("item") or g["zone"]})
            item = c2.selectbox("Item", item_opts, index=item_opts.index(g.get("item") or g["zone"]), format_func=label)
            qty = c3.selectbox("How much", QTYS, index=QTYS.index(g["qty_label"]) if g.get("qty_label") in QTYS else 1, format_func=label)
            if st.form_submit_button("✓ Confirm", type="primary"):
                try:
                    a.confirm(g["grab_id"], action, item, qty)
                    _load.clear()
                    st.toast(f"Confirmed: {label(action)} {label(qty)} of {label(item)}")
                    st.rerun()
                except ApiError as e:
                    st.error(str(e))

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

# ---------- trust ----------
st.subheader("What the agent said (and what it didn't)")
st.caption("Every text is fact-checked by d1 before it reaches you. Low scores get softened or blocked.")
try:
    msgs = load("messages", 50)
except ApiError as e:
    msgs = []
    st.error(str(e))
if msgs:
    counts = pd.Series([m["decision"] for m in msgs]).value_counts()
    m1, m2, m3 = st.columns(3)
    m1.metric("Sent", int(counts.get("sent", 0)))
    m2.metric("Softened", int(counts.get("softened", 0)))
    m3.metric("Blocked", int(counts.get("blocked", 0)))
    badge = {"sent": "🟢 sent", "softened": "🟡 softened", "blocked": "🔴 blocked"}
    st.dataframe(
        pd.DataFrame([{
            "When": datetime.fromisoformat(str(m["ts"]).replace("Z", "+00:00")).strftime("%a %-I:%M %p"),
            "Message": m["text"], "d1 score": m["d1_score"], "Decision": badge.get(m["decision"], m["decision"]),
        } for m in msgs]),
        hide_index=True, width="stretch",
        column_config={"d1 score": st.column_config.ProgressColumn("d1 score", min_value=0, max_value=1, format="%.2f"),
                       "Message": st.column_config.TextColumn(width="large")},
    )
else:
    st.caption("No agent messages yet.")
