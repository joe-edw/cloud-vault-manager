import streamlit as st
import pandas as pd
import io

# --- CONFIGURATION ---
DEFAULT_SHEET_URL = ""

st.set_page_config(page_title="Cloud Vault Manager", layout="wide")

# --- FINANCIAL LOGIC ---
def get_break_even(cost_basis):
    """Calculate Break-Even Floor with 13% platform fee margin."""
    return cost_basis / 0.87

def get_target_price(break_even_floor):
    """Calculate Target List Price with a 1.18 profit markup."""
    return break_even_floor * 1.18

def check_hard_hold(cost_basis, market_price):
    """Check if Market Price is below Cost Basis."""
    return market_price < cost_basis

# --- DATA FETCHING ---
def load_inventory_data(url):
    """Fetch CSV data directly from a Google Sheets CSV Export URL."""
    try:
        if not url:
            st.info("Please enter a Google Sheets CSV Export URL in the sidebar to begin.")
            return None
        
        if "/edit" in url:
            url = url.split("/edit")[0] + "/export?format=csv"
        elif "/export" not in url:
            st.error("Invalid URL format. Please use a standard Google Sheets link.")
            return None

        df = pd.read_csv(url)
        return df
    except Exception as e:
        st.error(f"Error loading data from Google Sheets: {e}")
        return None

# --- DATA PROCESSING ---
def process_sports_cards(df):
    """Maps specific sports card columns and cleans data."""
    # Hardcoded Mapping
    mapping = {
        'subject': 'Card Name',
        'My Cost': 'Cost Basis',
        'Market Price': 'Market Price',
        'AB': 'Listing Status',
        'ebay list': 'Listing Price'
    }
    
    # Try to find 'AB' or 'ebay list' if they are named slightly differently
    # But prioritize the requested mapping
    df = df.rename(columns=mapping)
    
    # Ensure all required columns exist for Tab 1 display
    display_cols = ['Year', 'Set', 'subject', 'Variety', 'Grade Issuer', 'Grade', 'My Cost']
    for col in display_cols:
        if col not in df.columns:
            # Check if we mapped it
            if col == 'subject' and 'Card Name' in df.columns:
                df['subject'] = df['Card Name']
            elif col == 'My Cost' and 'Cost Basis' in df.columns:
                df['My Cost'] = df['Cost Basis']
            else:
                df[col] = ""

    # Ensure Listing Status exists
    if 'Listing Status' not in df.columns:
        # Check if 'AB' is in the original columns if rename failed
        df['Listing Status'] = df.get('AB', "")

    # Clean numeric columns
    for col in ['My Cost', 'Market Price', 'Listing Price']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(r'[\$,]', '', regex=True), errors='coerce').fillna(0)
        else:
            df[col] = 0.0

    # Calculate Estimator Logic
    df['Break-Even Floor'] = df['My Cost'].apply(get_break_even)
    df['Target Price'] = df['Break-Even Floor'].apply(get_target_price)
    
    return df

# --- APP UI ---
st.title("📦 Cloud Vault Manager")

st.sidebar.header("Data Source")
sheet_url = st.sidebar.text_input("Google Sheets URL", value=DEFAULT_SHEET_URL)

if sheet_url:
    df_raw = load_inventory_data(sheet_url)
    
    if df_raw is not None:
        df = process_sports_cards(df_raw)
        
        tab1, tab2, tab3 = st.tabs(["1️⃣ Unlisted Vault", "2️⃣ Active eBay Listings", "3️⃣ Sold History"])
        
        with tab1:
            st.subheader("Inventory: Dynamic Price Estimator")
            # Filter: Column 'AB' (Listing Status) is blank OR contains a dash '-'
            unlisted_df = df[
                df['Listing Status'].isna() | 
                (df['Listing Status'].astype(str).str.strip() == "") | 
                (df['Listing Status'].astype(str).str.strip() == "-")
            ].copy()
            
            if not unlisted_df.empty:
                # Display metrics
                cols = st.columns(3)
                cols[0].metric("Vault Count", len(unlisted_df))
                cols[1].metric("Vault Cost", f"${unlisted_df['My Cost'].sum():,.2f}")
                cols[2].metric("Target Value", f"${unlisted_df['Target Price'].sum():,.2f}")
                
                st.dataframe(
                    unlisted_df[[
                        'Year', 'Set', 'subject', 'Variety', 'Grade Issuer', 'Grade', 
                        'My Cost', 'Break-Even Floor', 'Target Price'
                    ]].style.format({
                        'My Cost': '${:,.2f}',
                        'Break-Even Floor': '${:,.2f}',
                        'Target Price': '${:,.2f}'
                    }),
                    width="stretch"
                )
            else:
                st.info("No unlisted items found (all items have a status in column 'AB').")

        with tab2:
            st.subheader("Active Monitoring: Undervalued Alert Scanner")
            # Filter: Column 'AB' is 'Active'
            active_df = df[df['Listing Status'].astype(str).str.lower() == "active"].copy()
            
            if not active_df.empty:
                # 15% Undervalued Logic: Listing Price <= (Market Price * 0.85)
                # This identifies listings that are 15% or more BELOW the current market surge.
                active_df['Alert Status'] = active_df.apply(
                    lambda row: "⚠️ UNDERVALUED" if row['Listing Price'] <= (row['Market Price'] * 0.85) and row['Market Price'] > 0 else "✅ SAFE",
                    axis=1
                )
                
                undervalued_count = len(active_df[active_df['Alert Status'] == "⚠️ UNDERVALUED"])
                
                if undervalued_count > 0:
                    st.error(f"🚨 **CRITICAL ALERT:** {undervalued_count} active listings are severely undervalued (15%+ below market)!")
                    # Filter to show just the risky ones in the text alert area
                    risky_list = active_df[active_df['Alert Status'] == "⚠️ UNDERVALUED"]
                    for _, row in risky_list.iterrows():
                        st.warning(f"**{row['subject']}**: Listed at ${row['Listing Price']:.2f} | Market: **${row['Market Price']:.2f}**")
                else:
                    st.success("All active listings are within safe market margins.")

                # Display the grid with highlighting
                st.dataframe(
                    active_df[['Alert Status', 'subject', 'Listing Price', 'Market Price']].style.format({
                        'Listing Price': '${:,.2f}',
                        'Market Price': '${:,.2f}'
                    }).apply(
                        lambda x: ['background-color: #ffe699' if x['Alert Status'] == "⚠️ UNDERVALUED" else '' for i in x], axis=1
                    ),
                    width="stretch"
                )
            else:
                st.info("No items marked 'Active' in column 'AB'.")

        with tab3:
            st.subheader("Performance Tracking")
            # Filter: Column 'AB' is 'Sold'
            sold_df = df[df['Listing Status'].astype(str).str.lower() == "sold"].copy()
            
            if not sold_df.empty:
                st.metric("Total Realized Sales", f"${sold_df['Market Price'].sum():,.2f}")
                st.dataframe(
                    sold_df[['subject', 'My Cost', 'Market Price']].style.format({
                        'My Cost': '${:,.2f}',
                        'Market Price': '${:,.2f}'
                    }),
                    width="stretch"
                )
            else:
                st.info("No items marked 'Sold' in column 'AB'.")
    else:
        st.info("Awaiting spreadsheet data...")

st.sidebar.markdown("---")
st.sidebar.info("Financial logic: 13% Fee Floor | 1.18 Profit Markup")
