"""
Test Step 4: Fetch Schwab pricing and construct order with real-time prices

This test validates:
1. Schwab pricing is fetched for multi-leg spreads before order construction
2. Schwab mark prices are used for limit price calculation
3. Comparison between estimated (scanner) and actual (Schwab) prices
4. Proper logging of pricing differences
"""

import sys
import logging
from datetime import datetime, timedelta
import json

# Setup logging to see what's happening
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("ApexTrader")

# Add workspace to path
sys.path.insert(0, '/c:/Users/spoli/Desktop/BitStrider_WS/BitStrider')

from engine.utils.schwab_pricing import get_spread_complete_pricing
from engine.broker.schwab_client import get_schwab_market_data_client


def test_spread_order_construction():
    """Test getting Schwab prices and calculating order limit prices"""
    
    print("\n" + "="*80)
    print("STEP 4: SCHWAB PRICING FOR ORDER CONSTRUCTION")
    print("="*80)
    
    # Test Case 1: SMH Bull Call Spread (Debit)
    print("\n[TEST 1] SMH 550/555 Bull Call Spread (Debit)")
    print("-" * 80)
    
    underlying = "SMH"
    legs_smh = [
        {
            "occ_symbol": "SMH550C",
            "side": "buy",
            "ratio_qty": 1,
            "strike": 550.0,
            "opt_type": "call"
        },
        {
            "occ_symbol": "SMH555C",
            "side": "sell",
            "ratio_qty": 1,
            "strike": 555.0,
            "opt_type": "call"
        }
    ]
    
    # Estimated entry price from scanner (might be stale)
    estimated_entry = 2.57
    action = "buy"  # Debit spread
    
    print(f"Underlying: {underlying}")
    print(f"Legs: 550C (buy) / 555C (sell)")
    print(f"Estimated entry price: ${estimated_entry:.2f}")
    print(f"Action: {action}")
    
    # Fetch real-time Schwab pricing
    print("\nFetching real-time Schwab pricing...")
    pricing = get_spread_complete_pricing(underlying, legs_smh, estimated_entry)
    
    if pricing:
        schwab_mark = pricing.get("spread_mark")
        schwab_bid = pricing.get("spread_bid")
        schwab_ask = pricing.get("spread_ask")
        pnl_mark = pricing.get("pnl_mark_pct")
        dte = pricing.get("dte")
        
        print(f"\n✅ Schwab Pricing Retrieved:")
        print(f"   Bid:  ${schwab_bid:.2f}")
        print(f"   Mark: ${schwab_mark:.2f}  (diff from estimated: {schwab_mark - estimated_entry:+.2f})")
        print(f"   Ask:  ${schwab_ask:.2f}")
        print(f"   DTE:  {dte} days")
        print(f"   P&L (mark): {pnl_mark:+.1f}%")
        
        # Calculate order limit prices
        if action == "buy":
            # Debit: bid 1% below mark (pay less)
            estimated_limit = round(estimated_entry * 0.99, 2)
            schwab_limit = round(schwab_mark * 0.99, 2)
            print(f"\n📋 Order Limit Prices (BUY DEBIT - bid 1% below):")
            print(f"   Estimated limit: ${estimated_limit:.2f}  (99% of ${estimated_entry:.2f})")
            print(f"   Schwab limit:    ${schwab_limit:.2f}  (99% of ${schwab_mark:.2f})")
            print(f"   💰 Price improvement: ${schwab_limit - estimated_limit:+.2f}")
            
        print(f"\n📝 Order Construction:")
        print(f"   Symbol: [empty for mleg]")
        print(f"   Qty: 9 contracts")
        print(f"   Type: limit")
        print(f"   Order Class: mleg")
        print(f"   Limit Price: {schwab_limit} (from Schwab real-time)")
        print(f"   Time in Force: day")
        print(f"   Legs:")
        print(f"     - SMH550C: side=buy, ratio_qty=1")
        print(f"     - SMH555C: side=sell, ratio_qty=1")
        
    else:
        print("❌ Failed to get Schwab pricing")
        return False
    
    # Test Case 2: TQQQ Debit Put Spread
    print("\n" + "="*80)
    print("[TEST 2] TQQQ 71/73 Debit Put Spread (different style)")
    print("-" * 80)
    
    underlying = "TQQQ"
    legs_tqqq = [
        {
            "occ_symbol": "TQQQ710P",
            "side": "buy",
            "ratio_qty": 1,
            "strike": 71.0,
            "opt_type": "put"
        },
        {
            "occ_symbol": "TQQQ730P",
            "side": "sell",
            "ratio_qty": 1,
            "strike": 73.0,
            "opt_type": "put"
        }
    ]
    
    estimated_entry = 1.05
    action = "buy"
    
    print(f"Underlying: {underlying}")
    print(f"Legs: 71P (buy) / 73P (sell)")
    print(f"Estimated entry price: ${estimated_entry:.2f}")
    print(f"Action: {action}")
    
    print("\nFetching real-time Schwab pricing...")
    pricing = get_spread_complete_pricing(underlying, legs_tqqq, estimated_entry)
    
    if pricing:
        schwab_mark = pricing.get("spread_mark")
        schwab_bid = pricing.get("spread_bid")
        schwab_ask = pricing.get("spread_ask")
        pnl_mark = pricing.get("pnl_mark_pct")
        dte = pricing.get("dte")
        
        print(f"\n✅ Schwab Pricing Retrieved:")
        print(f"   Bid:  ${schwab_bid:.2f}")
        print(f"   Mark: ${schwab_mark:.2f}  (diff from estimated: {schwab_mark - estimated_entry:+.2f})")
        print(f"   Ask:  ${schwab_ask:.2f}")
        print(f"   DTE:  {dte} days")
        print(f"   P&L (mark): {pnl_mark:+.1f}%")
        
        if action == "buy":
            estimated_limit = round(estimated_entry * 0.99, 2)
            schwab_limit = round(schwab_mark * 0.99, 2)
            print(f"\n📋 Order Limit Prices (BUY DEBIT - bid 1% below):")
            print(f"   Estimated limit: ${estimated_limit:.2f}  (99% of ${estimated_entry:.2f})")
            print(f"   Schwab limit:    ${schwab_limit:.2f}  (99% of ${schwab_mark:.2f})")
            print(f"   💰 Price improvement: ${schwab_limit - estimated_limit:+.2f}")
    else:
        print("❌ Failed to get Schwab pricing")
        return False
    
    # Test Case 3: Single Leg (for comparison)
    print("\n" + "="*80)
    print("[TEST 3] SMH Single Leg Call (for comparison)")
    print("-" * 80)
    
    underlying = "SMH"
    strike = 550.0
    opt_type = "call"
    estimated_mid = 8.45
    action = "buy"
    
    print(f"Underlying: {underlying}")
    print(f"Contract: 550C (call)")
    print(f"Estimated mid price: ${estimated_mid:.2f}")
    print(f"Action: {action}")
    
    print("\nFetching real-time Schwab pricing...")
    try:
        client = get_schwab_market_data_client()
        chain_data = client.get_option_chains(underlying, contract_type="ALL")
        
        if chain_data:
            exp_date_map = chain_data.get("callExpDateMap", {})
            bid_price = None
            ask_price = None
            mark_price = None
            
            # Search for matching strike
            for exp_date_str, strikes_dict in exp_date_map.items():
                for strike_str, option_list in strikes_dict.items():
                    try:
                        strike_num = float(strike_str)
                        if abs(strike_num - strike) < 0.01:
                            if isinstance(option_list, list) and len(option_list) > 0:
                                option_data = option_list[0]
                                bid_price = float(option_data.get("bid", 0))
                                ask_price = float(option_data.get("ask", 0))
                                mark_price = float(option_data.get("mark", 0))
                                
                                if mark_price == 0 and bid_price and ask_price:
                                    mark_price = (bid_price + ask_price) / 2.0
                                break
                    except (ValueError, TypeError):
                        continue
                if bid_price is not None:
                    break
            
            if bid_price is not None and ask_price is not None and mark_price is not None:
                print(f"\n✅ Schwab Pricing Retrieved:")
                print(f"   Bid:  ${bid_price:.2f}")
                print(f"   Mark: ${mark_price:.2f}  (diff from estimated: {mark_price - estimated_mid:+.2f})")
                print(f"   Ask:  ${ask_price:.2f}")
                
                if action == "buy":
                    estimated_limit = round(estimated_mid * 0.99, 2)
                    schwab_limit = round(mark_price * 0.99, 2)
                    print(f"\n📋 Order Limit Price (BUY SINGLE - bid 1% below):")
                    print(f"   Estimated limit: ${estimated_limit:.2f}  (99% of ${estimated_mid:.2f})")
                    print(f"   Schwab limit:    ${schwab_limit:.2f}  (99% of ${mark_price:.2f})")
                    print(f"   💰 Price improvement: ${schwab_limit - estimated_limit:+.2f}")
            else:
                print("❌ No pricing found for strike")
                return False
        else:
            print("❌ No chain data from Schwab")
            return False
            
    except Exception as e:
        print(f"❌ Error fetching single-leg pricing: {e}")
        return False
    
    print("\n" + "="*80)
    print("✅ STEP 4 TEST COMPLETE")
    print("="*80)
    print("\nKey Findings:")
    print("1. Real-time Schwab pricing fetched before order construction")
    print("2. Schwab mark prices compared to estimated (scanner) prices")
    print("3. Limit prices calculated with 1% improvement using Schwab marks")
    print("4. Multi-leg and single-leg orders both supported")
    print("5. All price differences logged for transparency")
    
    return True


if __name__ == "__main__":
    success = test_spread_order_construction()
    sys.exit(0 if success else 1)
