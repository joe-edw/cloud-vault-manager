import streamlit as st
import pandas as pd
import requests

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
# PriceCharting live market value
# PriceCharting reuses video-game field names for cards. Mapping (verify in
# their API docs once your key is live):
#   loose-price       -> Ungraded / raw
#   graded-price      -> PSA 9
#   manual-only-price -> PSA 10
#   box-only-price    -> BGS 9.5
#   bgs-10-price      -> BGS 10
# Prices are returned in pennies (integer).
# ---------------------------------------------------------------------------
PRICECHARTING_URL = "https://www.pricecharting.com/api/product"

def price_field_for_grade(grade):
    g = str(grade).strip()
    if g == "10":
        return "manual-only-price"   # PSA 10
    if g in ("9", "9.5"):
        return "graded-price"        # PSA 9 (closest available tier)
    return "loose-price"             # raw / ungraded fallback

def build_query(row):
    parts = [str(row.get("Year", "")), str(row.get("Set", "")), str(row.get("subject", ""))]
    cardno = str(row.get("Card Number", "")).strip()
    if cardno and cardno != "-":
        parts.append("#" + cardno)
    return " ".join(p for p in parts if p and p not in ("-", "nan")).strip()

@st.cache_data(ttl=86400, show_spinner=False)
def pricecharting_lookup(query, grade, token):
    if not token or not query:
        return None
    try:
        resp = requests.get(PRICECHARTING_URL, params={"t": token, "q": query}, timeout=10)
        data = resp.json()
        if data.get("status") != "success":
            return None
        field = price_field_for_grade(grade)
        cents = data.get(field) or data.get("graded-price") or data.get("loose-price")
        if not cents:
            return None
        return cents / 100.0
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
st.sidebar.info("Fee buffer: 13% | Profit target: 18% | Undervalued trigger: 15% below market value")

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
        token = st.secrets.get("PRICECHARTING_TOKEN", "") if hasattr(st, "secrets") else ""
        if token:
            with st.spinner(f"Fetching live market values from PriceCharting for {len(active)} listings..."):
                active["Market Value"] = active.apply(
                    lambda r: pricecharting_lookup(build_query(r), r["Grade"], token), axis=1)
            matched = active["Market Value"].notna().sum()
            # Fall back to PSA Estimate where no PriceCharting match was found.
            active["Market Value"] = active["Market Value"].fillna(active["PSA Estimate"])
            st.caption(f"Live market value from PriceCharting for {matched} of {len(active)} listings; the rest fall back to PSA Estimate.")
        else:
            active["Market Value"] = active["PSA Estimate"]
            st.info("No PriceCharting API key found. Using PSA Estimate as market value. "
                    "Add PRICECHARTING_TOKEN under the app's Settings -> Secrets to enable live market values.")

        # How far below market value each listing is priced.
        # Positive % = listed below market; negative = listed above market.
        def under_market(r):
            if r["Market Value"] > 0:
                return (r["Market Value"] - r["Listing Price"]) / r["Market Value"] * 100
            return 0.0
        active["Under Market %"] = active.apply(under_market, axis=1)
        active["Alert"] = active["Under Market %"].apply(
            lambda p: "UNDERVALUED" if p >= 15 else "SAFE")
        active = active.sort_values("Under Market %", ascending=False)

        flagged = active[active["Alert"] == "UNDERVALUED"]
        safe = active[active["Alert"] == "SAFE"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Active Listings", f"{len(active):,}")
        c2.metric("Below Market (15%+)", f"{len(flagged):,}")
        c3.metric("Priced OK", f"{len(safe):,}")

        if not flagged.empty:
            st.warning(f"{len(flagged)} of {len(active)} listings are priced 15%+ below market value. Worst offenders are sorted to the top.")
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
