"""
Helper module for calculating multi-leg spread mark prices from Schwab market data.

Purpose: Get accurate bid/ask quotes for spreads/butterflies/condors from Schwab API
and calculate consolidated P&L instead of relying on individual leg quotes.
"""

import logging
from typing import Dict, Optional, Tuple
from datetime import datetime
from engine.broker.schwab_client import get_schwab_market_data_client

log = logging.getLogger("ApexTrader")



def get_spread_complete_pricing(
    underlying: str,
    legs: list,
    entry_price: float,
) -> Dict:
    """
    Get complete spread pricing: bid, mark, ask prices, DTE, and P&L metrics.
    
    Args:
        underlying: Underlying symbol (e.g., 'SMH', 'QQQ')
        legs: List of leg dicts with keys: occ_symbol, side, ratio_qty, strike
        entry_price: Entry premium (signed: + for debit, - for credit)
    
    Returns:
        Dict with keys:
        - spread_bid: What you'd GET to close position (most conservative)
        - spread_mark: Mid/theoretical value (MAIN P&L metric)
        - spread_ask: What you'd PAY to enter more
        - pnl_mark_pct: P&L % using mark price
        - pnl_ask_pct: P&L % using ask price  
        - pnl_bid_pct: P&L % using bid price
        - dte: Days to expiration (first leg's expiration)
        - expiration_date: Expiration date string
    """
    try:
        client = get_schwab_market_data_client()
        chain_data = client.get_option_chains(underlying, contract_type="ALL")
        
        if not chain_data or "putExpDateMap" not in chain_data:
            log.warning(f"[SCHWAB] No option chain data for {underlying}")
            return None
        
        # Extract bid/ask/mark and expiration date for each leg
        spread_bid = 0.0
        spread_ask = 0.0
        spread_mark = 0.0
        expiration_date = None
        
        for leg in legs:
            occ_sym = leg["occ_symbol"]
            side = leg["side"]  # "buy" or "sell"
            ratio = leg.get("ratio_qty", 1)
            strike = leg.get("strike")
            
            # Fallback: extract strike from OCC symbol if not provided
            if strike is None:
                import re
                try:
                    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ_sym)
                    if m:
                        strike = int(m.group(4)) / 1000.0
                except Exception:
                    pass
            
            if strike is None:
                log.warning(f"[SCHWAB] No strike price in leg for {occ_sym}")
                return None
            
            # Determine if CALL or PUT (prefer explicit opt_type if provided)
            if "opt_type" in leg:
                opt_type = leg["opt_type"].upper()
            else:
                opt_type = "CALL" if "C" in occ_sym or occ_sym.endswith("C") else "PUT"
            
            # Find this option in the chain
            exp_date_map = chain_data.get("callExpDateMap" if opt_type == "CALL" else "putExpDateMap", {})
            
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
                                
                                # Fallback if mark is 0
                                if mark_price == 0 and bid_price and ask_price:
                                    mark_price = (bid_price + ask_price) / 2.0
                                
                                # Capture expiration from first leg
                                if expiration_date is None:
                                    expiration_date = exp_date_str.split(":")[0]  # Format: "2026-05-09:0"
                                
                                break
                    except (ValueError, TypeError):
                        continue
                if bid_price is not None:
                    break
            
            if bid_price is None or ask_price is None:
                log.warning(f"[SCHWAB] No bid/ask for {occ_sym} at strike {strike}")
                return None
            
            # Apply sign (buy=+1, sell=-1) and ratio
            if side == "buy":
                # Long leg
                spread_ask += ask_price * ratio
                spread_bid += bid_price * ratio
                spread_mark += mark_price * ratio
            else:
                # Short leg (subtract)
                spread_ask -= bid_price * ratio
                spread_bid -= ask_price * ratio
                spread_mark -= mark_price * ratio
            
            log.debug(
                f"[SCHWAB] {occ_sym} strike={strike} {side:4s}: "
                f"bid=${bid_price:.2f} mid=${mark_price:.2f} ask=${ask_price:.2f}"
            )
        
        # For DEBIT spreads (entry_price > 0): clamp negative composite prices to $0
        # (negative composite is a pricing artifact — debit spreads can't go below $0).
        # For CREDIT spreads (entry_price < 0): a negative composite is VALID — it means
        # the cost-to-close exceeds the credit received (i.e., a losing position near max loss).
        # Clamping to 0 here would make a max-loss credit spread look like +100% profit.
        if entry_price >= 0:
            if spread_bid < 0:
                spread_bid = 0.0
            if spread_mark < 0:
                spread_mark = 0.0
            if spread_ask < 0:
                spread_ask = 0.0
        
        # Calculate DTE
        dte = None
        if expiration_date:
            try:
                exp_date_obj = datetime.strptime(expiration_date, "%Y-%m-%d")
                today = datetime.now()
                dte = (exp_date_obj - today).days
            except:
                dte = None
        
        # Calculate P&L % for each metric
        entry_price_abs = abs(entry_price)
        if entry_price_abs < 0.01:
            return None
        
        pnl_mark_pct = (spread_mark - entry_price) / entry_price_abs * 100
        pnl_bid_pct = (spread_bid - entry_price) / entry_price_abs * 100
        pnl_ask_pct = (spread_ask - entry_price) / entry_price_abs * 100
        
        result = {
            "spread_bid": spread_bid,
            "spread_mark": spread_mark,
            "spread_ask": spread_ask,
            "pnl_mark_pct": pnl_mark_pct,
            "pnl_bid_pct": pnl_bid_pct,
            "pnl_ask_pct": pnl_ask_pct,
            "dte": dte,
            "expiration_date": expiration_date,
            "entry_price": entry_price,
        }
        
        log.debug(
            f"[SCHWAB] {underlying} complete pricing - "
            f"bid=${spread_bid:.2f} mark=${spread_mark:.2f} ask=${spread_ask:.2f} | "
            f"mark_pnl={pnl_mark_pct:+.1f}% | dte={dte}d"
        )
        
        return result
    
    except Exception as e:
        log.warning(f"[SCHWAB] Error calculating complete spread pricing for {underlying}: {e}")
        return None


def get_spread_mark_from_schwab(
    underlying: str,
    legs: list,
    entry_price: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Calculate multi-leg spread mark/bid/ask prices from Schwab option chains.
    Uses Schwab's consolidated mark, bid, and ask prices.
    
    Returns the spread's MARK price (mid/theoretical value) for P&L calculation.
    
    Args:
        underlying: Underlying symbol (e.g., 'SMH', 'QQQ')
        legs: List of leg dicts with keys: occ_symbol, side, ratio_qty, strike
        entry_price: Entry premium (signed: + for debit, - for credit)
    
    Returns:
        Tuple of (current_mark_price, pnl_pct) or (None, None) if unable to calculate
        
    Spread Price Calculation (Bull Call Example):
        - Ask spread price = Ask(long) - Bid(short) = what you'd pay to enter
        - Bid spread price = Bid(long) - Ask(short) = what you'd get to exit
        - Mark spread price = Mark(long) - Mark(short) = mid (USED FOR P&L)
    """
    try:
        client = get_schwab_market_data_client()
        chain_data = client.get_option_chains(underlying, contract_type="ALL")
        
        if not chain_data or "putExpDateMap" not in chain_data:
            log.warning(f"[SCHWAB] No option chain data for {underlying}")
            return None, None
        
        # Extract bid/ask/mark for each leg
        spread_bid = 0.0
        spread_ask = 0.0
        spread_mark = 0.0
        
        for leg in legs:
            occ_sym = leg["occ_symbol"]
            side = leg["side"]  # "buy" or "sell"
            ratio = leg.get("ratio_qty", 1)
            strike = leg.get("strike")
            
            # Fallback: extract strike from OCC symbol if not provided
            if strike is None:
                import re
                try:
                    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ_sym)
                    if m:
                        strike = int(m.group(4)) / 1000.0
                except Exception:
                    pass
            
            if strike is None:
                log.warning(f"[SCHWAB] No strike price in leg for {occ_sym}")
                return None, None
            
            # Determine if CALL or PUT (prefer explicit opt_type if provided)
            if "opt_type" in leg:
                opt_type = leg["opt_type"].upper()
            else:
                opt_type = "CALL" if "C" in occ_sym or occ_sym.endswith("C") else "PUT"
            
            # Find this option in the chain
            exp_date_map = chain_data.get("callExpDateMap" if opt_type == "CALL" else "putExpDateMap", {})
            
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
                                
                                # Fallback if mark is 0
                                if mark_price == 0 and bid_price and ask_price:
                                    mark_price = (bid_price + ask_price) / 2.0
                                break
                    except (ValueError, TypeError):
                        continue
                if bid_price is not None:
                    break
            
            if bid_price is None or ask_price is None:
                log.warning(f"[SCHWAB] No bid/ask for {occ_sym} at strike {strike}")
                return None, None
            
            # Apply sign (buy=+1, sell=-1) and ratio
            sign = 1 if side == "buy" else -1
            
            # For spread calculations:
            # - Ask (entry cost): long uses ask, short uses bid
            # - Bid (exit proceeds): long uses bid, short uses ask  
            # - Mark (mid): uses mark
            
            if side == "buy":
                # Long leg
                spread_ask += ask_price * ratio
                spread_bid += bid_price * ratio
                spread_mark += mark_price * ratio
            else:
                # Short leg (subtract)
                spread_ask -= bid_price * ratio
                spread_bid -= ask_price * ratio
                spread_mark -= mark_price * ratio
            
            log.debug(
                f"[SCHWAB] {occ_sym} strike={strike} {side:4s}: "
                f"bid=${bid_price:.2f} mid=${mark_price:.2f} ask=${ask_price:.2f}"
            )
        
        # Use MARK price for P&L (mid/theoretical value of position)
        current_price = spread_mark
        
        # Validate price
        if current_price < 0:
            log.warning(
                f"[SCHWAB] {underlying} spread mark was negative (${current_price:.2f}), "
                f"clamping to $0.00."
            )
            current_price = 0.0
        
        # Calculate P&L % using the mark price (mid/fair value)
        entry_price_abs = abs(entry_price)
        if entry_price_abs < 0.01:
            return None, None
        
        pnl_pct = (current_price - entry_price) / entry_price_abs * 100
        
        log.debug(
            f"[SCHWAB] {underlying} spread prices - "
            f"bid=${spread_bid:.2f} mark=${spread_mark:.2f} ask=${spread_ask:.2f} | "
            f"using mark=${current_price:.2f} (entry: ${entry_price:.2f}, pnl: {pnl_pct:+.1f}%)"
        )
        
        return current_price, pnl_pct
    
    except Exception as e:
        log.warning(f"[SCHWAB] Error calculating spread mark for {underlying}: {e}")
        return None, None


def get_single_leg_mark_from_schwab(
    underlying: str,
    strike: float,
    opt_type: str,
    entry_price: float,
    side: str = "buy",
) -> Tuple[Optional[float], Optional[float]]:
    """
    Get mark price for a single option leg from Schwab chains.
    
    Args:
        underlying: Underlying symbol
        strike: Strike price
        opt_type: "CALL" or "PUT"
        entry_price: Entry premium (signed)
        side: "buy" or "sell"
    
    Returns:
        Tuple of (current_mark, pnl_pct)
    """
    try:
        client = get_schwab_market_data_client()
        chain_data = client.get_option_chains(underlying, contract_type=opt_type)
        
        if not chain_data:
            return None, None
        
        exp_date_map = chain_data.get("callExpDateMap" if opt_type == "CALL" else "putExpDateMap", {})
        
        bid_price = None
        ask_price = None
        
        for exp_date_str, strikes_dict in exp_date_map.items():
            for strike_str, option_list in strikes_dict.items():
                try:
                    strike_num = float(strike_str)
                    if abs(strike_num - strike) < 0.01:
                        if isinstance(option_list, list) and len(option_list) > 0:
                            option_data = option_list[0]
                            bid_price = float(option_data.get("bid", 0))
                            ask_price = float(option_data.get("ask", 0))
                            break
                except (ValueError, TypeError):
                    continue
            if bid_price is not None:
                break
        
        if bid_price is None or ask_price is None:
            return None, None
        
        # Use bid for sell, ask for buy
        current_mark = bid_price if side == "sell" else ask_price
        
        entry_price_abs = abs(entry_price)
        if entry_price_abs < 0.01:
            return None, None
        
        pnl_pct = (current_mark - entry_price) / entry_price_abs * 100
        
        return current_mark, pnl_pct
    
    except Exception as e:
        log.warning(f"[SCHWAB] Error calculating single leg mark: {e}")
        return None, None
