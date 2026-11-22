"""
Streamlit rendering of the dashboard payload (``dashboard.build``): summary cards, the date -> shift -> room roster,
faculty duty distribution, eligibility report, unfilled positions and the validation report, plus Excel / PDF downloads.

Only formatting lives here - every number comes from the payload.  Streamlit is imported lazily so the rest of the
package works without it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

def _width_kwargs() -> dict:
    """Streamlit renamed use_container_width -> width="stretch"; support both."""

    try:
        import inspect
        import streamlit as st
        return {"width": "stretch"} if "width" in inspect.signature(st.dataframe).parameters else {"use_container_width": True}
    except Exception:
        return {}


_W = _width_kwargs()
_STATUS_ICON = {"Passed": "✅", "Failed": "❌", "Not verified": "⚠️", "Not applicable": "➖"}


def render(payload: Dict[str, Any], handler: Any = None, key: str = "exam") -> None:
    import pandas as pd
    import streamlit as st

    p = payload
    (st.success if p["complete"] and p["validation_overall"] == "Passed" else st.warning)(p["headline"])
    if p.get("notes"):
        with st.expander("Notes about this allocation", expanded=False):
            for n in p["notes"]:
                st.markdown(f"- {n}")
    with st.expander("What I understood from your request", expanded=False):
        st.dataframe(pd.DataFrame(p["understood"]).rename(columns={"item": "Item", "value": "Value", "source": "Source"}),
                     hide_index=True, **_W)

    cards = p["cards"]
    for start in range(0, len(cards), 5):
        cols = st.columns(5)
        for col, c in zip(cols, cards[start:start + 5]):
            col.metric(c["label"], c["value"])

    un = p["unfilled"]
    if un["rows"]:
        st.error(f"⚠️ {sum(r[5] for r in un['rows'])} position(s) in {len(un['rows'])} room-session(s) are **unfilled** - "
                 f"{p['feasibility_text']}. See the “Unfilled positions” tab.")

    n_unf = len(un["rows"])
    tabs = st.tabs(["📅 Duty roster", "📊 Faculty duty distribution", f"⚠️ Unfilled positions ({n_unf})" if n_unf else "✅ Unfilled positions",
                    "🧾 Faculty eligibility report", f"{_STATUS_ICON.get(p['validation_overall'], '')} Validation report"])

    with tabs[0]:
        for day in p["roster"]:
            st.subheader(f"{day['label']}  ·  {day['weekday']}")
            for sh in day["shifts"]:
                tag = f"  ·  ⚠️ {sh['unfilled']} unfilled" if sh["unfilled"] else ""
                st.markdown(f"**{sh['label']} — {sh['time']}**{tag}")
                df = pd.DataFrame(sh["rows"], columns=_unique(sh["headers"]))
                st.dataframe(_highlight(df), hide_index=True, **_W,
                             height=min(38 + 35 * len(df), 520))

    with tabs[1]:
        st.caption(p["counting_note"])
        d = p["duty_distribution"]
        df = pd.DataFrame(d["rows"], columns=d["headers"])
        sort = st.selectbox("Sort by", ["New duties (most first)", "Total duties (most first)", "Total duties (fewest first)", "Faculty name"],
                            key=f"{key}_sort_{p['allocation_id']}")
        order = {"New duties (most first)": ("New duties", False), "Total duties (most first)": ("Total duties", False),
                 "Total duties (fewest first)": ("Total duties", True), "Faculty name": ("Faculty", True)}[sort]
        st.dataframe(df.sort_values(order[0], ascending=order[1], kind="stable") if len(df) else df, hide_index=True, **_W)
        if len(df):
            c1, c2, c3 = st.columns(3)
            c1.metric("Faculty assigned", len(df))
            c2.metric("New duties (min – max)", f"{int(df['New duties'].min())} – {int(df['New duties'].max())}")
            c3.metric("Total duties after (min – max)", f"{int(df['Total duties'].min())} – {int(df['Total duties'].max())}")

    with tabs[2]:
        if un["rows"]:
            st.warning(f"{p['feasibility_text']}. Rooms below could not be fully staffed.")
            st.dataframe(pd.DataFrame(un["rows"], columns=un["headers"]), hide_index=True, **_W)
            if un.get("needed_extra"):
                st.info(f"About {un['needed_extra']} more eligible faculty member(s) would be needed in the tightest session.")
        else:
            st.success("Every required position is filled.")

    with tabs[3]:
        e = p["eligibility"]
        st.caption("Faculty who were considered but not selected, with the verified reason. “Unverified” means the data needed "
                   "to rule a conflict out is missing - such faculty are never treated as free.")
        df = pd.DataFrame(e["rows"], columns=e["headers"])
        pick = st.multiselect("Show status", sorted(df["Status"].unique()) if len(df) else [], default=None, key=f"{key}_st_{p['allocation_id']}")
        st.dataframe(df[df["Status"].isin(pick)] if pick else df, hide_index=True, **_W, height=420)

    with tabs[4]:
        v = pd.DataFrame([{"": _STATUS_ICON.get(c["status"], ""), "Check": c["check"], "Status": c["status"], "Detail": c["detail"]}
                          for c in p["validation"]])
        st.dataframe(v, hide_index=True, **_W)
        st.caption("“Passed” appears only when the condition was actually verified against the data.")

    if handler is not None:
        cache = st.session_state.setdefault("_exam_exports", {})
        c1, c2, _ = st.columns([1, 1, 3])
        for col, kind, label in ((c1, "xlsx", "⬇️ Download Excel"), (c2, "pdf", "⬇️ Download PDF")):
            ck = (p["allocation_id"], p["version"], kind)
            if ck not in cache:
                try:
                    cache[ck] = handler.export(kind, p["allocation_id"])
                except Exception as error:        # never break the chat over an export problem
                    col.caption(f"{kind.upper()} export unavailable ({error})")
                    continue
            data, name, mime = cache[ck]
            col.download_button(label, data, file_name=name, mime=mime, key=f"{key}_dl_{kind}_{p['allocation_id']}")
    st.caption("Reply **approve** to save this roster, or describe a change (for example “exclude <faculty name>”, “make it 3 per room”).")


def _unique(headers):
    seen, out = {}, []
    for h in headers:
        seen[h] = seen.get(h, 0) + 1
        out.append(h if seen[h] == 1 else f"{h} ")      # st.dataframe needs unique column names
    return out


def _highlight(df):
    def row(r):
        return ["background-color: #fde8e8; color: #9b1c1c" if "UNFILLED" in str(v) else "" for v in r]
    try:
        return df.style.apply(row, axis=1)
    except Exception:
        return df