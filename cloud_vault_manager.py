import streamlit as st
import pandas as pd
import requests
import base64

st.set_page_config(page_title="Cloud Vault Manager", layout="wide")

def break_even(cost):
    return cost / 0.87

def target_price(be):
    return be * 1.18

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

def process(df):
    df = df.rename(columns={"Subject": "subject"})
    for col in ["Year", "Set", "Card Number", "subject", "Variety", "Grade Issuer", "Grade",
                "My Cost", "PSA Estimate", "Listing Price", "Listing Status",
                "Sold Status", "Sold Price", "Sold Fees", "Sold Proceeds"]:
        if col not in df.columns:
            df[col] = ""
    for col in ["My Cost", "PSA Estimate", "Listing Price",
                "Sold Price", "Sold Fees", "Sold Proceeds"]:
        df[col] = clean_money(df[col])
    df["Grade"] = pd.to_numeric(df["Grade"], errors="coerce")
    df["Grade"] = df["Grade"].apply(lambda x: str(int(x)) if pd.notna(x) and x == int(x) else (str(x) if pd.notna(x) else ""))
    df["Listing Status"] = df["Listing Status"].astype(str).str.strip()
    df["Sold Status"] = df["Sold Status"].astype(str).str.strip()
    df["Variety"] = df["Variety"].astype(str).str.strip().replace("-", "")
    df["Break-Even Floor"] = df["My Cost"].apply(break_even)
    df["Target Price"] = df["Break-Even Floor"].apply(target_price)
    return df

def row_highlight(row):
    color = "background-color: #ffe699" if row["Alert"] == "UNDERVALUED" else ""
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
st.sidebar.header("Data Source")
sheet_url = st.sidebar.text_input("Google Sheets URL", placeholder="Paste your Google Sheets link here...")
if st.sidebar.button("Refresh Data"):
    st.cache_data.clear()
st.sidebar.markdown("---")
st.sidebar.info("Fee buffer: 13% | Profit target: 18% | Undervalued = listed below market value by the trigger %")

if not sheet_url:
    st.info("Paste your Google Sheets URL in the sidebar to get started.")
    st.stop()

raw = load_sheet(sheet_url)
if raw is None:
    st.stop()

df = process(raw)

tab1, tab2, tab3 = st.tabs(["Unlisted Vault", "Active eBay Listings", "Sold History"])

with tab1:
    st.subheader("Inventory: Dynamic Price Estimator")
    unlisted = df[df["Listing Status"].isin(["-", "", "nan"])].copy()
    if unlisted.empty:
        st.info("No unlisted items found.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Vault Count", f"{len(unlisted):,}")
        c2.metric("Total Cost", f"${unlisted['My Cost'].sum():,.2f}")
        c3.metric("Target Value", f"${unlisted['Target Price'].sum():,.2f}")
        st.dataframe(
            unlisted[["Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                      "My Cost", "Break-Even Floor", "Target Price"]]
            .style.format({"My Cost": MONEY, "Break-Even Floor": MONEY, "Target Price": MONEY}),
            use_container_width=True)

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
        token = ebay_token(secret("EBAY_APP_ID"), secret("EBAY_CERT_ID"))
        if token:
            with st.spinner(f"Fetching live eBay market values for {len(active)} listings..."):
                active["Market Value"] = active.apply(
                    lambda r: ebay_market_value(build_query(r), token), axis=1)
            matched = int(active["Market Value"].notna().sum())
            active["Market Value"] = active["Market Value"].fillna(active["PSA Estimate"])
            trigger = 20  # asking prices run high vs. final sale, so use a wider margin
            st.caption(f"Live eBay market value (median of active comps) for {matched} of {len(active)} "
                       f"listings; the rest fall back to PSA Estimate. Flagging when listed {trigger}%+ below market.")
        else:
            active["Market Value"] = active["PSA Estimate"]
            trigger = 15
            st.info("No eBay API keys found. Using PSA Estimate as market value. "
                    "Add EBAY_APP_ID and EBAY_CERT_ID under the app's Settings -> Secrets to enable live eBay values.")

        # How far below market value each listing is priced.
        # Positive % = listed below market; negative = listed above market.
        def under_market(r):
            if r["Market Value"] > 0:
                return (r["Market Value"] - r["Listing Price"]) / r["Market Value"] * 100
            return 0.0
        active["Under Market %"] = active.apply(under_market, axis=1)
        active["Alert"] = active["Under Market %"].apply(
            lambda p: "UNDERVALUED" if p >= trigger else "SAFE")
        active = active.sort_values("Under Market %", ascending=False)

        flagged = active[active["Alert"] == "UNDERVALUED"]
        safe = active[active["Alert"] == "SAFE"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Active Listings", f"{len(active):,}")
        c2.metric(f"Below Market ({trigger}%+)", f"{len(flagged):,}")
        c3.metric("Priced OK", f"{len(safe):,}")

        if not flagged.empty:
            st.warning(f"{len(flagged)} of {len(active)} listings are priced {trigger}%+ below market value. Worst offenders are sorted to the top.")
        else:
            st.success("All active listings are within safe market margins.")
        st.dataframe(
            active[["Alert", "Under Market %", "Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                    "My Cost", "Break-Even Floor", "Target Price", "Listing Price", "Market Value", "PSA Estimate"]]
            .style.format({"My Cost": MONEY, "Break-Even Floor": MONEY, "Target Price": MONEY,
                           "Listing Price": MONEY, "Market Value": MONEY, "PSA Estimate": MONEY,
                           "Under Market %": "{:.0f}%"})
            .apply(row_highlight, axis=1),
            use_container_width=True)

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
        st.dataframe(
            sold[["subject", "My Cost", "Sold Price", "Sold Fees", "Net Payout", "Net Profit", "ROI %"]]
            .style.format({"My Cost": MONEY, "Sold Price": MONEY, "Sold Fees": MONEY,
                           "Net Payout": MONEY, "Net Profit": MONEY, "ROI %": PCT}),
            use_container_width=True)
