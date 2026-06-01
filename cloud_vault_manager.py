import streamlit as st
import pandas as pd
import requests
import base64

st.set_page_config(page_title="Cloud Vault Manager", layout="wide")

def break_even(cost):
    return cost / 0.87

def target_price(be):
    return be * 1.18

def suggested_list(market_value, cost, premium_pct):
    # Modest premium above market value, proportional to the card's value.
    # Floored at the break-even price (cost / 0.87) so the post-fee bottom line never goes negative.
    by_market = market_value * (1 + premium_pct / 100.0)
    return max(by_market, break_even(cost))

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
    for col in ["Year", "Set", "Card Number", "subject", "Variety", "Grade Issuer", "Grade",
                "My Cost", "PSA Estimate", "Card Ladder Value", "Listing Price", "Listing Status",
                "Sold Status", "Sold Price", "Sold Fees", "Sold Proceeds"]:
        if col not in df.columns:
            df[col] = ""
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
premium_pct = st.sidebar.slider("Listing premium above market value (%)", 0, 30, 10,
                                help="Suggested List = Market Value x (1 + this %), never below break-even.")
st.sidebar.info("Fee buffer: 13% | Market value = Card Ladder | Suggested List keeps a positive post-fee margin")

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
        unlisted["Suggested List"] = unlisted.apply(
            lambda r: suggested_list(r["Market Value"], r["My Cost"], premium_pct), axis=1)
        unlisted["Proj Net"] = unlisted["Suggested List"] * 0.87 - unlisted["My Cost"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Vault Count", f"{len(unlisted):,}")
        c2.metric("Total Cost", f"${unlisted['My Cost'].sum():,.2f}")
        c3.metric("Suggested List Total", f"${unlisted['Suggested List'].sum():,.2f}")
        c4.metric("Proj. Net Profit", f"${unlisted['Proj Net'].sum():,.2f}")
        st.dataframe(
            unlisted[["Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                      "My Cost", "Break-Even Floor", "Market Value", "Suggested List", "Proj Net"]]
            .style.format({"My Cost": MONEY, "Break-Even Floor": MONEY, "Market Value": MONEY,
                           "Suggested List": MONEY, "Proj Net": MONEY}),
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
        cl_matched = int((active["Card Ladder Value"] > 0).sum())
        st.caption(f"Market value = Card Ladder for {cl_matched} of {len(active)} listings (rest fall back to PSA Estimate). "
                   f"Suggested List = market x (1 + {premium_pct}% premium), floored at break-even so Proj Net stays >= 0.")

        active["Under Market %"] = active.apply(
            lambda r: (r["Market Value"] - r["Listing Price"]) / r["Market Value"] * 100 if r["Market Value"] > 0 else 0.0,
            axis=1)
        active["Alert"] = active["Under Market %"].apply(lambda p: "UNDERVALUED" if p >= 15 else "SAFE")
        active["Suggested List"] = active.apply(
            lambda r: suggested_list(r["Market Value"], r["My Cost"], premium_pct), axis=1)
        active["Proj Net"] = active["Suggested List"] * 0.87 - active["My Cost"]
        active = active.sort_values("Under Market %", ascending=False)

        flagged = active[active["Alert"] == "UNDERVALUED"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Active Listings", f"{len(active):,}")
        c2.metric("Below Market (15%+)", f"{len(flagged):,}")
        c3.metric("Priced OK", f"{len(active) - len(flagged):,}")

        if not flagged.empty:
            st.warning(f"{len(flagged)} of {len(active)} listings are priced 15%+ below Card Ladder market value. "
                       f"Compare 'Listing Price' to 'Suggested List' to reprice. Worst offenders sorted to top.")
        else:
            st.success("All active listings are within safe market margins.")
        st.dataframe(
            active[["Alert", "Under Market %", "Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                    "My Cost", "Listing Price", "Market Value", "Suggested List", "Proj Net"]]
            .style.format({"My Cost": MONEY, "Listing Price": MONEY, "Market Value": MONEY,
                           "Suggested List": MONEY, "Proj Net": MONEY, "Under Market %": "{:.0f}%"})
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
