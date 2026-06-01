import streamlit as st
import pandas as pd
import requests
import base64
import io

st.set_page_config(page_title="Cloud Vault Manager", layout="wide")

def break_even(cost):
    return cost / 0.87

def target_price(be):
    return be * 1.18

def suggested_list(market_value, cost, premium_pct, catchup_rate=0.0):
    # Modest premium above market value (proportional to the card's value), plus an optional
    # catch-up surcharge (also proportional to market value) to recover prior portfolio losses.
    # Floored at the break-even price (cost / 0.87) so the post-fee line never goes negative.
    by_market = market_value * (1 + premium_pct / 100.0 + catchup_rate)
    return max(by_market, break_even(cost))

def is_hold(market_value, cost, premium_pct):
    # "Underwater": a market-competitive price (market + premium) can't even cover break-even,
    # so listing now would mean either a loss or pricing so far above market it won't sell. Hold instead.
    return market_value > 0 and market_value * (1 + premium_pct / 100.0) < break_even(cost)

def to_csv_url(url):
    if not url:
        return None
    if "/edit" in url:
        return url.split("/edit")[0] + "/export?format=csv"
    if "/export" in url:
        return url
    return None

@st.cache_data(ttl=120)
def load_sheet(url):
    csv_url = to_csv_url(url)
    if csv_url is None:
        st.error("Invalid Google Sheets URL.")
        return None
    try:
        return pd.read_csv(csv_url)
    except Exception as exc:
        st.error(f"Failed to load sheet: {exc}")
        return None

def clean_money(series):
    return pd.to_numeric(
        series.astype(str).str.replace(r"[$,\s]", "", regex=True),
        errors="coerce",
    ).fillna(0.0)

def norm_cert(c):
    c = str(c).strip()
    if c.endswith(".0"):
        c = c[:-2]
    return c.lstrip("0") or c

@st.cache_data(ttl=3600)
def load_card_ladder_values():
    # Bundled Card Ladder values (cert -> value), refreshed from a Card Ladder export.
    try:
        cl = pd.read_csv("card_ladder_values.csv", dtype=str)
        return {norm_cert(c): float(v) for c, v in
                zip(cl["Cert Number"], cl["Card Ladder Value"]) if v}
    except Exception:
        return {}

def process(df):
    df = df.rename(columns={"Subject": "subject"})
    for col in ["Year", "Set", "Card Number", "Cert Number", "subject", "Variety", "Grade Issuer", "Grade",
                "My Cost", "PSA Estimate", "Card Ladder Value", "Listing Price", "Listing Status",
                "Sold Status", "Sold Price", "Sold Fees", "Sold Proceeds"]:
        if col not in df.columns:
            df[col] = ""
    df["Cert Number"] = df["Cert Number"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    for col in ["My Cost", "PSA Estimate", "Card Ladder Value", "Listing Price",
                "Sold Price", "Sold Fees", "Sold Proceeds"]:
        df[col] = clean_money(df[col])
    df["Grade"] = pd.to_numeric(df["Grade"], errors="coerce")
    df["Grade"] = df["Grade"].apply(lambda x: str(int(x)) if pd.notna(x) and x == int(x) else (str(x) if pd.notna(x) else ""))
    df["Listing Status"] = df["Listing Status"].astype(str).str.strip()
    df["Sold Status"] = df["Sold Status"].astype(str).str.strip()
    df["Variety"] = df["Variety"].astype(str).str.strip().replace("-", "")
    df["Break-Even Floor"] = df["My Cost"].apply(break_even)
    df["Target Price"] = df["Break-Even Floor"].apply(target_price)

    # Merge bundled Card Ladder values by cert number (overrides the blank column).
    cl_map = load_card_ladder_values()
    if cl_map and "Cert Number" in df.columns:
        df["Card Ladder Value"] = df["Cert Number"].map(
            lambda c: cl_map.get(norm_cert(c), 0.0))

    # Canonical market value: Card Ladder where available, else PSA Estimate.
    df["Market Value"] = df["Card Ladder Value"].where(df["Card Ladder Value"] > 0, df["PSA Estimate"])
    return df

def row_highlight(row):
    status = row.get("Alert", row.get("Action", ""))
    if status == "UNDERVALUED":
        color = "background-color: #ffe699"   # yellow: listed below market, reprice up
    elif status == "HOLD":
        color = "background-color: #f8d7da"   # red: market below break-even, don't list
    else:
        color = ""
    return [color] * len(row)

# ---------------------------------------------------------------------------
# eBay Browse API — free, official. Market value = median asking price of
# active comparable listings, matched by card + grade.
# Needs EBAY_APP_ID and EBAY_CERT_ID in Streamlit secrets (free dev account).
# ---------------------------------------------------------------------------
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

def secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""

def check_password():
    # Password gate. If no APP_PASSWORD secret is set, the app stays open.
    expected = secret("APP_PASSWORD")
    if not expected:
        return True
    if st.session_state.get("auth_ok"):
        return True
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if pw == expected:
            st.session_state["auth_ok"] = True
            return True
        st.error("Incorrect password")
    return False

@st.cache_data(ttl=6000, show_spinner=False)
def ebay_token(app_id, cert_id):
    if not app_id or not cert_id:
        return None
    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    try:
        resp = requests.post(
            EBAY_OAUTH_URL,
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=10)
        return resp.json().get("access_token")
    except Exception:
        return None

def build_query(row):
    parts = [str(row.get("Year", "")), str(row.get("Set", "")), str(row.get("subject", ""))]
    cardno = str(row.get("Card Number", "")).strip()
    if cardno and cardno not in ("-", "nan"):
        parts.append("#" + cardno)
    grader = str(row.get("Grade Issuer", "")).strip()
    grade = str(row.get("Grade", "")).strip()
    if grader and grader not in ("-", "nan") and grade:
        parts.append(f"{grader} {grade}")
    return " ".join(p for p in parts if p and p not in ("-", "nan")).strip()

@st.cache_data(ttl=86400, show_spinner=False)
def ebay_market_value(query, token):
    if not token or not query:
        return None
    try:
        resp = requests.get(
            EBAY_BROWSE_URL,
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            params={"q": query, "limit": 20, "filter": "buyingOptions:{FIXED_PRICE}"},
            timeout=10)
        items = resp.json().get("itemSummaries", []) or []
        prices = []
        for it in items:
            p = it.get("price", {})
            if p.get("currency") == "USD":
                try:
                    prices.append(float(p["value"]))
                except (KeyError, ValueError, TypeError):
                    pass
        if not prices:
            return None
        prices.sort()
        n = len(prices)
        mid = n // 2
        return prices[mid] if n % 2 else (prices[mid - 1] + prices[mid]) / 2
    except Exception:
        return None

MONEY = "${:,.2f}"
PCT = "{:.1f}%"

st.title("Cloud Vault Manager")
if not check_password():
    st.stop()
st.sidebar.header("Data Source")
sheet_url = st.sidebar.text_input("Google Sheets URL", value=secret("SHEET_URL"),
                                  placeholder="Paste your Google Sheets link here...")
if st.sidebar.button("Refresh Data"):
    st.cache_data.clear()
st.sidebar.markdown("---")
premium_pct = st.sidebar.slider("Listing premium above market value (%)", 0, 30, 10,
                                help="Suggested List = Market Value x (1 + this %), never below break-even.")
recover = st.sidebar.checkbox("Recover past losses (catch-up billing)", value=True,
                              help="Spread prior realized net losses across listable inventory, proportional to "
                                   "market value, so the total bottom line (incl. past sales) returns to positive.")
cap_pct = st.sidebar.slider("Max total markup over market (%)", premium_pct, 60, max(premium_pct, 20),
                            help="Caps how far above market value the premium + catch-up can push a price, "
                                 "to protect sell-through. Catch-up uses whatever room is left under this cap.")
st.sidebar.info("Fee buffer: 13% | Market value = Card Ladder | Suggested List keeps a positive post-fee margin")

if not sheet_url:
    st.info("Paste your Google Sheets URL in the sidebar to get started.")
    st.stop()

raw = load_sheet(sheet_url)
if raw is None:
    st.stop()

df = process(raw)

# ---------------------------------------------------------------------------
# Portfolio bottom line + loss-recovery (catch-up billing)
# ---------------------------------------------------------------------------
def realized_pnl(d):
    s = d[d["Sold Status"].str.lower() == "sold"]
    payout = s["Sold Proceeds"].where(s["Sold Proceeds"] > 0, s["Sold Price"] * 0.87)
    return float((payout - s["My Cost"]).sum())

def listable_mask(d):
    active_m = ((d["Listing Status"] == "Fixed Price")
                & (~d["Sold Status"].str.lower().isin(["sold", "processing payout"]))
                & (d["Sold Price"] == 0))
    return active_m | d["Listing Status"].isin(["-", "", "nan"])

realized = realized_pnl(df)
deficit = max(0.0, -realized)
pool = df[listable_mask(df)].copy()
pool = pool[~pool.apply(lambda r: is_hold(r["Market Value"], r["My Cost"], premium_pct), axis=1)]
pool_mv = float(pool["Market Value"].sum())
# Full catch-up that would recover the entire deficit (grossed up for the fee)...
full_rate = (deficit / 0.87) / pool_mv if (deficit > 0 and pool_mv > 0) else 0.0
# ...capped by the room left under the max total markup, to protect sell-through.
cap_gap = max(0.0, cap_pct / 100.0 - premium_pct / 100.0)
catchup_rate = min(full_rate, cap_gap) if (recover and deficit > 0) else 0.0
recovered = pool_mv * catchup_rate * 0.87

def proj_net(r):
    cu = 0.0 if is_hold(r["Market Value"], r["My Cost"], premium_pct) else catchup_rate
    return suggested_list(r["Market Value"], r["My Cost"], premium_pct, cu) * 0.87 - r["My Cost"]
pool_proj_net = float(pool.apply(proj_net, axis=1).sum()) if len(pool) else 0.0

st.subheader("Portfolio Bottom Line")
m1, m2, m3, m4 = st.columns(4)
m1.metric("Realized P&L (sold)", f"${realized:,.2f}")
m2.metric("Catch-up Applied", f"{catchup_rate*100:.0f}%")
m3.metric("Proj. Net (if pool sells)", f"${pool_proj_net:,.2f}")
m4.metric("Total Bottom Line", f"${realized + pool_proj_net:,.2f}")
if recover and deficit > 0 and catchup_rate > 0:
    if catchup_rate < full_rate - 1e-9:
        st.caption(f"Catch-up capped at {cap_pct}% total markup: recovering ${recovered:,.2f} of the "
                   f"${deficit:,.2f} loss across this pool (remaining ${deficit - recovered:,.2f} keeps recovering "
                   f"as you sell and relist). Raise the cap for faster recovery, lower it for better sell-through.")
    else:
        st.caption(f"Recovering the full ${deficit:,.2f} loss via a {catchup_rate*100:.0f}% catch-up across "
                   f"{len(pool)} cards. Heads-up: markups this high may slow sell-through.")
elif deficit > 0:
    st.caption(f"Past realized loss of ${deficit:,.2f} is NOT being recovered in pricing (catch-up off).")

exports = {}  # collected per-tab views for the combined Excel export
tab1, tab2, tab3 = st.tabs(["Unlisted Vault", "Active eBay Listings", "Sold History"])

with tab1:
    st.subheader("Inventory: Dynamic Price Estimator")
    unlisted = df[df["Listing Status"].isin(["-", "", "nan"])].copy()
    if unlisted.empty:
        st.info("No unlisted items found.")
    else:
        unlisted["Action"] = unlisted.apply(
            lambda r: "HOLD" if is_hold(r["Market Value"], r["My Cost"], premium_pct) else "LIST", axis=1)
        unlisted["Suggested List"] = unlisted.apply(
            lambda r: suggested_list(r["Market Value"], r["My Cost"], premium_pct,
                                     0.0 if r["Action"] == "HOLD" else catchup_rate), axis=1)
        unlisted["Proj Net"] = unlisted["Suggested List"] * 0.87 - unlisted["My Cost"]
        listable = unlisted[unlisted["Action"] == "LIST"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Vault Count", f"{len(unlisted):,}")
        c2.metric("Ready to List", f"{len(listable):,}")
        c3.metric("Hold (underwater)", f"{(unlisted['Action'] == 'HOLD').sum():,}")
        c4.metric("Proj. Net (listable)", f"${listable['Proj Net'].sum():,.2f}")
        st.caption("Row key:  🔴 HOLD = market is below your break-even, don't list yet  ·  "
                   "⬜ LIST = ready to list at Suggested List")
        view = unlisted[["Action", "Cert Number", "Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                         "My Cost", "Break-Even Floor", "Market Value", "Suggested List", "Proj Net"]]
        st.dataframe(
            view.style.format({"My Cost": MONEY, "Break-Even Floor": MONEY, "Market Value": MONEY,
                               "Suggested List": MONEY, "Proj Net": MONEY})
            .apply(row_highlight, axis=1),
            use_container_width=True)
        exports["Unlisted Vault"] = view
        st.download_button("Download this view (CSV)", view.to_csv(index=False),
                           "unlisted_vault.csv", "text/csv", key="dl_unlisted")

with tab2:
    st.subheader("Active Monitoring: Undervalued Alert Scanner")
    # Truly active = listed at a fixed price AND not yet sold.
    # (Sold cards keep Listing Status "Fixed Price", so also exclude anything with a sold status/price.)
    active = df[
        (df["Listing Status"] == "Fixed Price")
        & (~df["Sold Status"].str.lower().isin(["sold", "processing payout"]))
        & (df["Sold Price"] == 0)
    ].copy()
    if active.empty:
        st.info("No items currently listed.")
    else:
        cl_matched = int((active["Card Ladder Value"] > 0).sum())
        st.caption(f"Market value = Card Ladder for {cl_matched} of {len(active)} listings (rest fall back to PSA Estimate). "
                   f"Suggested List = market x (1 + {premium_pct}% premium), floored at break-even so Proj Net stays >= 0.")

        active["Under Market %"] = active.apply(
            lambda r: (r["Market Value"] - r["Listing Price"]) / r["Market Value"] * 100 if r["Market Value"] > 0 else 0.0,
            axis=1)
        active["Suggested List"] = active.apply(
            lambda r: suggested_list(r["Market Value"], r["My Cost"], premium_pct,
                                     0.0 if is_hold(r["Market Value"], r["My Cost"], premium_pct) else catchup_rate), axis=1)
        active["Proj Net"] = active["Suggested List"] * 0.87 - active["My Cost"]
        active["Alert"] = active.apply(
            lambda r: "HOLD" if is_hold(r["Market Value"], r["My Cost"], premium_pct)
            else ("UNDERVALUED" if r["Under Market %"] >= 15 else "SAFE"), axis=1)
        active = active.sort_values("Under Market %", ascending=False)

        flagged = active[active["Alert"] == "UNDERVALUED"]
        hold = active[active["Alert"] == "HOLD"]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active Listings", f"{len(active):,}")
        c2.metric("Below Market (15%+)", f"{len(flagged):,}")
        c3.metric("Priced OK", f"{len(active) - len(flagged) - len(hold):,}")
        c4.metric("Hold (underwater)", f"{len(hold):,}")

        if not flagged.empty:
            st.warning(f"{len(flagged)} of {len(active)} listings are priced 15%+ below Card Ladder market value. "
                       f"Compare 'Listing Price' to 'Suggested List' to reprice. Worst offenders sorted to top.")
        else:
            st.success("All active listings are within safe market margins.")
        if not hold.empty:
            st.info(f"{len(hold)} active listing(s) are below break-even at market (red rows) - "
                    f"consider delisting and holding until Card Ladder value recovers.")
        st.caption("Row key:  🟡 UNDERVALUED = listed 15%+ below market, reprice up to Suggested List  ·  "
                   "🔴 HOLD = market below break-even, consider delisting  ·  ⬜ SAFE = priced OK")
        view = active[["Alert", "Under Market %", "Cert Number", "Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                       "My Cost", "Listing Price", "Market Value", "Suggested List", "Proj Net"]]
        st.dataframe(
            view.style.format({"My Cost": MONEY, "Listing Price": MONEY, "Market Value": MONEY,
                               "Suggested List": MONEY, "Proj Net": MONEY, "Under Market %": "{:.0f}%"})
            .apply(row_highlight, axis=1),
            use_container_width=True)
        exports["Active eBay Listings"] = view
        st.download_button("Download this view (CSV)", view.to_csv(index=False),
                           "active_listings.csv", "text/csv", key="dl_active")

with tab3:
    st.subheader("Performance Tracking: Realised Returns")
    sold = df[df["Sold Status"].str.lower() == "sold"].copy()
    if sold.empty:
        st.info("No sold items found.")
    else:
        sold["Net Payout"] = sold["Sold Proceeds"].where(sold["Sold Proceeds"] > 0, sold["Sold Price"] * 0.87)
        sold["Net Profit"] = sold["Net Payout"] - sold["My Cost"]
        sold["ROI %"] = (sold["Net Profit"] / sold["My Cost"].replace(0, float("nan"))) * 100
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Cards Sold", f"{len(sold):,}")
        c2.metric("Total Cost In", f"${sold['My Cost'].sum():,.2f}")
        c3.metric("Net Payout Out", f"${sold['Net Payout'].sum():,.2f}")
        c4.metric("Net Profit", f"${sold['Net Profit'].sum():,.2f}")
        c5.metric("Avg ROI", f"{sold['ROI %'].mean():.1f}%")
        view = sold[["Cert Number", "subject", "My Cost", "Sold Price", "Sold Fees", "Net Payout", "Net Profit", "ROI %"]]
        st.dataframe(
            view.style.format({"My Cost": MONEY, "Sold Price": MONEY, "Sold Fees": MONEY,
                               "Net Payout": MONEY, "Net Profit": MONEY, "ROI %": PCT}),
            use_container_width=True)
        exports["Sold History"] = view
        st.download_button("Download this view (CSV)", view.to_csv(index=False),
                           "sold_history.csv", "text/csv", key="dl_sold")

# Combined Excel export: all sections as separate sheets in one workbook.
if exports:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, v in exports.items():
            v.to_excel(writer, sheet_name=name[:31], index=False)
    st.sidebar.markdown("---")
    st.sidebar.download_button(
        "Download all sections (Excel)", buf.getvalue(),
        "cloud_vault_manager.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_all")
