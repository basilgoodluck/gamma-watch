"""Sanity check: Alpaca paper account + SPY options chain.

python scripts/check_connection.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from alpaca.trading.requests import GetOptionContractsRequest

from agent.config import ALPACA_API_KEY, ALPACA_BASE_URL, ALPACA_SECRET_KEY, UNDERLYING_SYMBOL

PAPER = "paper" in ALPACA_BASE_URL


def main() -> None:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER)

    account = trading_client.get_account()
    options_level = getattr(account, "options_trading_level", None) or getattr(
        account, "options_approved_level", None
    )

    print("=== Account ===")
    print(f"status:              {account.status}")
    print(f"buying_power:        {account.buying_power}")
    print(f"options_trading_level: {options_level}")
    if options_level is not None and int(options_level) < 3:
        print(
            f"WARNING: expected Level 3 options approval by default on paper "
            f"accounts, got level {options_level}. Flagging per step01 instructions "
            f"— not working around this silently."
        )

    contracts_req = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING_SYMBOL],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=date.today().isoformat(),
        expiration_date_lte=(date.today() + timedelta(days=14)).isoformat(),
        limit=1000,
    )
    contracts_resp = trading_client.get_option_contracts(contracts_req)
    contracts = contracts_resp.option_contracts

    if not contracts:
        print(f"\nNo option contracts found for {UNDERLYING_SYMBOL} in the next 14 days.")
        return

    nearest_expiry = min(c.expiration_date for c in contracts)
    nearest_contracts = [c for c in contracts if c.expiration_date == nearest_expiry]

    print(f"\n=== {UNDERLYING_SYMBOL} options chain ===")
    print(f"nearest expiry:      {nearest_expiry}")
    print(f"contracts at expiry: {len(nearest_contracts)}")

    option_data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    chain = option_data_client.get_option_chain(
        OptionChainRequest(
            underlying_symbol=UNDERLYING_SYMBOL,
            expiration_date=nearest_expiry,
        )
    )

    stock_data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    underlying_trade = stock_data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=UNDERLYING_SYMBOL)
    )[UNDERLYING_SYMBOL]
    underlying_price = underlying_trade.price
    print(f"underlying price:    {underlying_price}")

    sample_contract = min(nearest_contracts, key=lambda c: abs(float(c.strike_price) - underlying_price))
    sample_symbol = sample_contract.symbol
    snapshot = chain.get(sample_symbol)
    print(f"\n=== Sample contract (nearest-the-money): {sample_symbol} ===")
    if snapshot is None:
        print("no snapshot returned for sample contract")
    else:
        quote = snapshot.latest_quote
        print(f"bid:  {quote.bid_price if quote else 'n/a'}")
        print(f"ask:  {quote.ask_price if quote else 'n/a'}")
        iv = snapshot.implied_volatility
        if iv is not None:
            print(f"iv:   {iv}")
        else:
            print(
                "iv:   n/a (OPRA market data agreement not signed on this account - "
                "indicative feed has no greeks/IV. step03 will compute IV via "
                "Black-Scholes from bid/ask instead, per decision on 2026-09-01.)"
            )


if __name__ == "__main__":
    main()
