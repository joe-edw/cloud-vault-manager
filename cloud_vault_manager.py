import streamlit as st
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Cloud Vault Manager", layout="wide")

# ---------------------------------------------------------------------------
# FINANCIAL HELPERS
# ---------------------------------------------------------------------------
def break_even(cost: float) -> float:
    return cost / 0.87

def target_price(be: float) -> float:
    return be * 1.18

# ---------------------------------------------------------------------------
# DATA LAYER
# ---------------------------------------------------------------------------
def to_csv_url(url: str) -> str | None:
    if not url:
        return None
    if "/edit" in url:
        return url.split("/edit")[0] + "/export?format=csv"
    if "/export" in url:
        return url
    return None

@st.cache_data(ttl=120)
def load_sheet(url: str) -> pd.DataFrame | None:
    csv_url = to_csv_url(url)
    if csv_url is None:
        st.error("Invalid Google Sheets URL.")
        return None
    try:
        return pd.read_csv(csv_url)
    except Exception as exc:
        st.error(f"Failed to load sheet: {exc}")
        return None

def clean_money(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(r"[\$,\s]", "", regex=True),
        errors="coerce",
    ).fillna(0.0)

def process(df: pd.DataFrame) -> pd.DataFrame:
    # Rename to internal names
    df = df.rename(columns={"Subject": "subject"})

    # Guarantee all required columns exist
    for col in ["Year", "Set", "subject", "Variety", "Grade Issuer", "Grade",
                "My Cost", "PSA Estimate", "Listing Price", "Listing Status",
                "Sold Status", "Sold Price", "Sold Fees", "Sold Proceeds"]:
        if col not in df.columns:
            df[col] = ""

    # Clean money columns
    for col in ["My Cost", "PSA Estimate", "Listing Price",
                "Sold Price", "Sold Fees", "Sold Proceeds"]:
        df[col] = clean_money(df[col])

    # Clean grade — integer display
    df["Grade"] = pd.to_numeric(df["Grade"], errors="coerce")
    df["Grade"] = df["Grade"].apply(lambda x: int(x) if pd.notna(x) and x == int(x) else x)

    # Normalise text columns
    df["Listing Status"] = df["Listing Status"].astype(str).str.strip()
    df["Sold Status"]    = df["Sold Status"].astype(str).str.strip()
    df["Variety"]        = df["Variety"].astype(str).str.strip().replace("-", "")

    # Pre-compute vault pricing
    df["Break-Even Floor"] = df["My Cost"].apply(break_even)
    df["Target Price"]     = df["Break-Even Floor"].apply(target_price)

    return df

# ---------------------------------------------------------------------------
# STYLING
# ---------------------------------------------------------------------------
MONEY = "${:,.2f}"
PCT   = "{:.1f}%"

def row_highlight(row: pd.Series) -> list[str]:
    color = "background-color: #ffe699" if row["Alert"] == "⚠️ UNDERVALUED" else ""
    return [color] * len(row)

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
st.title("📦 Cloud Vault Manager")
st.sidebar.header("Data Source")
sheet_url = st.sidebar.text_input(
    "Google Sheets URL",
    placeholder="Paste your Google Sheets link here…"
)

if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()

st.sidebar.markdown("---")
st.sidebar.info("Fee buffer: 13% | Profit target: 18% | Undervalued trigger: 15% below PSA Estimate")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if not sheet_url:
    st.info("Paste your Google Sheets URL in the sidebar to get started.")
    st.stop()

raw = load_sheet(sheet_url)
if raw is None:
    st.stop()

df = process(raw)

tab1, tab2, tab3 = st.tabs([
    "1️⃣ Unlisted Vault",
    "2️⃣ Active eBay Listings",
    "3️⃣ Sold History",
])

# ---------------------------------------------------------------------------
# TAB 1 — UNLISTED VAULT
# Listing Status is '-' (dash) = not yet listed
# ---------------------------------------------------------------------------
with tab1:
    st.subheader("Inventory: Dynamic Price Estimator")

    unlisted = df[
        df["Listing Status"].isin(["-", "", "nan"])
    ].copy()

    if unlisted.empty:
        st.info("No unlisted items found.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Vault Count",  f"{len(unlisted):,}")
        c2.metric("Total Cost",   f"${unlisted['My Cost'].sum():,.2f}")
        c3.metric("Target Value", f"${unlisted['Target Price'].sum():,.2f}")

        st.dataframe(
            unlisted[[
                "Year", "Set", "subject", "Variety",
                "Grade Issuer", "Grade",
                "My Cost", "Break-Even Floor", "Target Price",
            ]].style.format({
                "My Cost":          MONEY,
                "Break-Even Floor": MONEY,
                "Target Price":     MONEY,
            }),
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
# TAB 2 — ACTIVE EBAY LISTINGS
# Listing Status = 'Fixed Price' means currently listed on eBay
# Market value = PSA Estimate column
# ---------------------------------------------------------------------------
with tab2:
    st.subheader("Active Monitoring: Undervalued Alert Scanner")

    active = df[df["Listing Status"] == "Fixed Price"].copy()

    if active.empty:
        st.info("No items currently listed (no 'Fixed Price' entries in Listing Status).")
    else:
        active["Alert"] = active.apply(
            lambda r: "⚠️ UNDERVALUED"
            if r["Listing Price"] <= (r["PSA Estimate"] * 0.85) and r["PSA Estimate"] > 0
            else "✅ SAFE",
            axis=1,
        )

        flagged = active[active["Alert"] == "⚠️ UNDERVALUED"]

        if not flagged.empty:
            st.error(f"🚨 **CRITICAL ALERT:** {len(flagged)} listing(s) are 15%+ below PSA Estimate!")
            for _, row in flagged.iterrows():
                st.warning(
                    f"**{row['subject']}** — Listed: ${row['Listing Price']:,.2f} | "
                    f"PSA Est: **${row['PSA Estimate']:,.2f}** | "
                    f"Gap: ${row['PSA Estimate'] - row['Listing Price']:,.2f}"
                )
        else:
            st.success("All active listings are within safe market margins.")

        display = active[["Alert", "subject", "Listing Price", "PSA Estimate"]].copy()

        st.dataframe(
            display.style
                .format({"Listing Price": MONEY, "PSA Estimate": MONEY})
                .apply(row_highlight, axis=1),
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
# TAB 3 — SOLD HISTORY
# Sold Status = 'eBay' — use Sold Price + Sold Proceeds (fees already deducted)
# ---------------------------------------------------------------------------
with tab3:
    st.subheader("Performance Tracking: Realised Returns")

    sold = df[df["Sold Status"].str.lower() == "ebay"].copy()

    if sold.empty:
        st.info("No sold items found (no 'eBay' entries in Sold Status).")
    else:
        # Use sheet's own Sold Proceeds where available; fall back to 13% fee calc
        sold["Net Payout"] = sold["Sold Proceeds"].where(
            sold["Sold Proceeds"] > 0,
            sold["Sold Price"] * 0.87
        )
        sold["Net Profit"] = sold["Net Payout"] - sold["My Cost"]
        sold["ROI %"]      = (
            sold["Net Profit"] / sold["My Cost"].replace(0, float("nan"))
        ) * 100

        total_cost   = sold["My Cost"].sum()
        total_payout = sold["Net Payout"].sum()
        total_profit = sold["Net Profit"].sum()
        avg_roi      = sold["ROI %"].mean()
        cards_sold   = len(sold)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Cards Sold",     f"{cards_sold:,}")
        c2.metric("Total Cost In",  f"${total_cost:,.2f}")
        c3.metric("Net Payout Out", f"${total_payout:,.2f}")
        c4.metric("Net Profit",     f"${total_profit:,.2f}", delta=f"${total_profit:,.2f}")
        c5.metric("Avg ROI",        f"{avg_roi:.1f}%",       delta=f"{avg_roi:.1f}%")

        st.dataframe(
            sold[["subject", "My Cost", "Sold Price", "Sold Fees", "Net Payout", "Net Profit", "ROI %"]]
                .style.format({
                    "My Cost":    MONEY,
                    "Sold Price": MONEY,
                    "Sold Fees":  MONEY,
                    "Net Payout": MONEY,
                    "Net Profit": MONEY,
                    "ROI %":      PCT,
                }),
            use_container_width=True,
        )
