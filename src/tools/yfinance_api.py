"""Free data provider backed by yfinance.

Mirrors the public functions in src.tools.api with the same Pydantic models,
so the two providers are interchangeable. Fundamentals come from the annual
statements (~4 years of history). Valuation ratios that need a price
(P/E, P/B, EV/EBITDA, ...) come from the current Ticker.info snapshot and are
only attached to the most recent period — do not use them for backtests.
"""

import datetime
import logging
import math

import yfinance as yf

from src.data.cache import get_cache
from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)

_cache = get_cache()


def _safe(value) -> float | None:
    """Convert a raw statement value to float, mapping NaN/inf/None to None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _row(df, names: list[str]):
    """Return the first matching row (a Series indexed by period) from a statement."""
    if df is None or df.empty:
        return None
    for name in names:
        if name in df.index:
            return df.loc[name]
    return None


def _at(series, column) -> float | None:
    if series is None:
        return None
    try:
        return _safe(series.get(column))
    except Exception:
        return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _growth(series, column, prior_column) -> float | None:
    current, prior = _at(series, column), _at(series, prior_column)
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


def get_prices(ticker: str, start_date: str, end_date: str, api_key: str = None) -> list[Price]:
    # Never persist a series that includes today: the last bar is a live
    # partial and would freeze quotes for the rest of the day.
    cacheable = end_date < datetime.date.today().isoformat()
    cache_key = f"yf_{ticker}_{start_date}_{end_date}"
    if cacheable and (cached_data := _cache.get_prices(cache_key)):
        return [Price(**price) for price in cached_data]

    # +2 days: yfinance parses the range in the exchange timezone, and a UTC
    # server date can lag the Sydney session by a day.
    end_exclusive = (datetime.date.fromisoformat(end_date) + datetime.timedelta(days=2)).isoformat()
    try:
        df = yf.Ticker(ticker).history(start=start_date, end=end_exclusive, interval="1d", auto_adjust=False)
    except Exception as e:
        logger.warning("yfinance price fetch failed for %s: %s", ticker, e)
        return []
    if df is None or df.empty:
        return []

    prices = []
    for index, row in df.iterrows():
        open_, close, high, low = (_safe(row.get(c)) for c in ("Open", "Close", "High", "Low"))
        if close is None:
            continue
        volume = _safe(row.get("Volume"))
        prices.append(
            Price(
                open=open_ if open_ is not None else close,
                close=close,
                high=high if high is not None else close,
                low=low if low is not None else close,
                volume=int(volume) if volume is not None else 0,
                time=index.date().isoformat(),
            )
        )

    if not prices:
        return []

    _cache.set_prices(cache_key, [p.model_dump() for p in prices])
    return prices


def get_adjusted_closes(ticker: str, start_date: str, end_date: str) -> list[Price]:
    """Dividend- and split-adjusted closes — for grading total returns.

    Regular get_prices shows real trading prices (right for charts); grading
    a call across an ex-dividend date needs the adjusted series or a payout
    eats the flat band.
    """
    end_exclusive = (datetime.date.fromisoformat(end_date) + datetime.timedelta(days=2)).isoformat()
    try:
        df = yf.Ticker(ticker).history(start=start_date, end=end_exclusive, interval="1d", auto_adjust=True)
    except Exception as e:
        logger.warning("yfinance adjusted fetch failed for %s: %s", ticker, e)
        return []
    if df is None or df.empty:
        return []
    prices = []
    for index, row in df.iterrows():
        close = _safe(row.get("Close"))
        if close is None:
            continue
        prices.append(Price(open=close, close=close, high=close, low=close, volume=0,
                            time=index.date().isoformat()))
    return prices


# Statement rows used by both metrics and line items. Each entry lists the
# yfinance row labels to try in order, because labels vary across companies.
_INCOME_ROWS = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "gross_profit": ["Gross Profit"],
    "operating_income": ["Operating Income"],
    "operating_expense": ["Operating Expense"],
    "net_income": ["Net Income", "Net Income Common Stockholders"],
    "research_and_development": ["Research And Development"],
    "ebit": ["EBIT"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "interest_expense": ["Interest Expense"],
    "earnings_per_share": ["Diluted EPS", "Basic EPS"],
    "cost_of_revenue": ["Cost Of Revenue"],
}

_BALANCE_ROWS = {
    "cash_and_equivalents": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "current_assets": ["Current Assets"],
    "current_liabilities": ["Current Liabilities"],
    "inventory": ["Inventory"],
    "total_assets": ["Total Assets"],
    "total_liabilities": ["Total Liabilities Net Minority Interest"],
    "shareholders_equity": ["Stockholders Equity", "Common Stock Equity"],
    "total_debt": ["Total Debt"],
    "working_capital": ["Working Capital"],
    "goodwill_and_intangible_assets": ["Goodwill And Other Intangible Assets", "Goodwill"],
    "intangible_assets": ["Other Intangible Assets"],
    "outstanding_shares": ["Ordinary Shares Number", "Share Issued"],
}

_CASHFLOW_ROWS = {
    "free_cash_flow": ["Free Cash Flow"],
    "capital_expenditure": ["Capital Expenditure"],
    "depreciation_and_amortization": ["Depreciation And Amortization", "Depreciation Amortization Depletion"],
    "dividends_and_other_cash_distributions": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    "issuance_or_purchase_of_equity_shares": ["Repurchase Of Capital Stock", "Net Common Stock Issuance"],
    "operating_cash_flow": ["Operating Cash Flow"],
}


def _load_statements(ticker: str):
    tkr = yf.Ticker(ticker)
    try:
        income, balance, cashflow = tkr.income_stmt, tkr.balance_sheet, tkr.cashflow
    except Exception as e:
        logger.warning("yfinance statements fetch failed for %s: %s", ticker, e)
        return tkr, None, None, None
    return tkr, income, balance, cashflow


def _report_periods(income, balance, end_date: str) -> list:
    """Period-end timestamps present in the statements, newest first, up to end_date."""
    columns = []
    for df in (income, balance):
        if df is not None and not df.empty:
            columns.extend(df.columns)
    cutoff = datetime.date.fromisoformat(end_date)
    unique = {c for c in columns if c.date() <= cutoff}
    return sorted(unique, reverse=True)


def _info(tkr) -> dict:
    try:
        return tkr.info or {}
    except Exception:
        return {}


_fx_cache: dict[str, tuple[float, float]] = {}  # pair -> (fetched_at_ts, rate)


def _fx_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Spot FX rate from_ccy -> to_ccy via Yahoo (cached ~1h)."""
    import time as _time

    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return 1.0
    pair = f"{from_ccy}{to_ccy}=X"
    hit = _fx_cache.get(pair)
    if hit and _time.time() - hit[0] < 3600:
        return hit[1]
    try:
        series = yf.Ticker(pair).history(period="5d", interval="1d")
        rate = float(series["Close"].dropna().iloc[-1])
    except Exception:
        return hit[1] if hit else None
    _fx_cache[pair] = (_time.time(), rate)
    return rate


def _market_caps_in_statement_currency(info: dict) -> tuple[float | None, float | None, str]:
    """Market cap and enterprise value converted into the statement currency.

    Yahoo quotes market cap in the listing currency (e.g. AUD for BHP.AX)
    while statements may be in another (USD for BHP). Mixing them makes
    valuation gaps and FCF yields wrong by the FX rate, so everything that
    sits next to statement figures gets converted here.
    """
    quote_ccy = info.get("currency") or "USD"
    fin_ccy = info.get("financialCurrency") or quote_ccy
    rate = _fx_rate(quote_ccy, fin_ccy)
    market_cap = _safe(info.get("marketCap"))
    enterprise_value = _safe(info.get("enterpriseValue"))
    if rate is None:
        return None, None, fin_ccy  # cannot convert: better absent than wrong
    mc = market_cap * rate if market_cap is not None else None
    ev = enterprise_value * rate if enterprise_value is not None else None
    return mc, ev, fin_ccy


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[FinancialMetrics]:
    cache_key = f"yf_{ticker}_{period}_{end_date}_{limit}"
    if cached_data := _cache.get_financial_metrics(cache_key):
        return [FinancialMetrics(**metric) for metric in cached_data]

    tkr, income, balance, cashflow = _load_statements(ticker)
    periods = _report_periods(income, balance, end_date)
    if not periods:
        return []
    info = _info(tkr)
    info_market_cap, info_enterprise_value, currency = _market_caps_in_statement_currency(info)

    rows = {}
    for name, labels in _INCOME_ROWS.items():
        rows[name] = _row(income, labels)
    for name, labels in _BALANCE_ROWS.items():
        rows[name] = _row(balance, labels)
    for name, labels in _CASHFLOW_ROWS.items():
        rows[name] = _row(cashflow, labels)

    metrics: list[FinancialMetrics] = []
    for i, col in enumerate(periods[:limit]):
        prior = periods[i + 1] if i + 1 < len(periods) else None

        revenue = _at(rows["revenue"], col)
        net_income = _at(rows["net_income"], col)
        equity = _at(rows["shareholders_equity"], col)
        total_assets = _at(rows["total_assets"], col)
        total_debt = _at(rows["total_debt"], col)
        ebit = _at(rows["ebit"], col)
        shares = _at(rows["outstanding_shares"], col)
        fcf = _at(rows["free_cash_flow"], col)
        current_assets = _at(rows["current_assets"], col)
        current_liabilities = _at(rows["current_liabilities"], col)
        inventory = _at(rows["inventory"], col)
        cash = _at(rows["cash_and_equivalents"], col)
        cost_of_revenue = _at(rows["cost_of_revenue"], col)
        interest_expense = _at(rows["interest_expense"], col)
        dividends_paid = _at(rows["dividends_and_other_cash_distributions"], col)
        operating_cash_flow = _at(rows["operating_cash_flow"], col)

        invested_capital = None
        if equity is not None and total_debt is not None:
            invested_capital = equity + total_debt

        book_value_growth = None
        prior_equity = _at(rows["shareholders_equity"], prior) if prior is not None else None
        if equity is not None and prior_equity not in (None, 0):
            book_value_growth = (equity - prior_equity) / abs(prior_equity)

        quick_assets = None
        if current_assets is not None:
            quick_assets = current_assets - (inventory or 0)

        latest = i == 0
        market_cap = info_market_cap if latest else None

        metrics.append(
            FinancialMetrics(
                ticker=ticker,
                report_period=col.date().isoformat(),
                period=period,
                currency=currency,
                market_cap=market_cap,
                enterprise_value=info_enterprise_value if latest else None,
                price_to_earnings_ratio=_safe(info.get("trailingPE")) if latest else None,
                price_to_book_ratio=_safe(info.get("priceToBook")) if latest else None,
                price_to_sales_ratio=_safe(info.get("priceToSalesTrailing12Months")) if latest else None,
                enterprise_value_to_ebitda_ratio=_safe(info.get("enterpriseToEbitda")) if latest else None,
                enterprise_value_to_revenue_ratio=_safe(info.get("enterpriseToRevenue")) if latest else None,
                free_cash_flow_yield=_ratio(fcf, market_cap),
                peg_ratio=_safe(info.get("trailingPegRatio")) if latest else None,
                gross_margin=_ratio(_at(rows["gross_profit"], col), revenue),
                operating_margin=_ratio(_at(rows["operating_income"], col), revenue),
                net_margin=_ratio(net_income, revenue),
                return_on_equity=_ratio(net_income, equity),
                return_on_assets=_ratio(net_income, total_assets),
                return_on_invested_capital=_ratio(ebit, invested_capital),
                asset_turnover=_ratio(revenue, total_assets),
                inventory_turnover=_ratio(cost_of_revenue, inventory),
                receivables_turnover=None,
                days_sales_outstanding=None,
                operating_cycle=None,
                working_capital_turnover=_ratio(revenue, _at(rows["working_capital"], col)),
                current_ratio=_ratio(current_assets, current_liabilities),
                quick_ratio=_ratio(quick_assets, current_liabilities),
                cash_ratio=_ratio(cash, current_liabilities),
                operating_cash_flow_ratio=_ratio(operating_cash_flow, current_liabilities),
                debt_to_equity=_ratio(total_debt, equity),
                debt_to_assets=_ratio(total_debt, total_assets),
                interest_coverage=_ratio(ebit, abs(interest_expense) if interest_expense else None),
                revenue_growth=_growth(rows["revenue"], col, prior),
                earnings_growth=_growth(rows["net_income"], col, prior),
                book_value_growth=book_value_growth,
                earnings_per_share_growth=_growth(rows["earnings_per_share"], col, prior),
                free_cash_flow_growth=_growth(rows["free_cash_flow"], col, prior),
                operating_income_growth=_growth(rows["operating_income"], col, prior),
                ebitda_growth=_growth(rows["ebitda"], col, prior),
                payout_ratio=_ratio(abs(dividends_paid) if dividends_paid else None, net_income),
                earnings_per_share=_at(rows["earnings_per_share"], col),
                book_value_per_share=_ratio(equity, shares),
                free_cash_flow_per_share=_ratio(fcf, shares),
            )
        )

    if not metrics:
        return []

    _cache.set_financial_metrics(cache_key, [m.model_dump() for m in metrics])
    return metrics


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[LineItem]:
    tkr, income, balance, cashflow = _load_statements(ticker)
    periods = _report_periods(income, balance, end_date)
    if not periods:
        return []
    info = _info(tkr)
    currency = info.get("financialCurrency") or info.get("currency") or "USD"

    def resolve(name: str, col) -> float | None:
        if name in _INCOME_ROWS:
            return _at(_row(income, _INCOME_ROWS[name]), col)
        if name in _BALANCE_ROWS:
            return _at(_row(balance, _BALANCE_ROWS[name]), col)
        if name in _CASHFLOW_ROWS:
            return _at(_row(cashflow, _CASHFLOW_ROWS[name]), col)
        # Ratios some agents request as line items
        if name == "gross_margin":
            return _ratio(resolve("gross_profit", col), resolve("revenue", col))
        if name == "operating_margin":
            return _ratio(resolve("operating_income", col), resolve("revenue", col))
        if name == "debt_to_equity":
            return _ratio(resolve("total_debt", col), resolve("shareholders_equity", col))
        if name == "return_on_invested_capital":
            equity, debt = resolve("shareholders_equity", col), resolve("total_debt", col)
            invested = equity + debt if equity is not None and debt is not None else None
            return _ratio(resolve("ebit", col), invested)
        if name == "book_value_per_share":
            return _ratio(resolve("shareholders_equity", col), resolve("outstanding_shares", col))
        return None

    results = []
    for col in periods[:limit]:
        values = {name: resolve(name, col) for name in line_items}
        results.append(
            LineItem(
                ticker=ticker,
                report_period=col.date().isoformat(),
                period=period,
                currency=currency,
                **values,
            )
        )
    return results


def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[InsiderTrade]:
    cache_key = f"yf_{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    if cached_data := _cache.get_insider_trades(cache_key):
        return [InsiderTrade(**trade) for trade in cached_data]

    try:
        df = yf.Ticker(ticker).insider_transactions
    except Exception as e:
        logger.warning("yfinance insider fetch failed for %s: %s", ticker, e)
        return []
    if df is None or df.empty:
        return []

    trades = []
    for _, row in df.iterrows():
        date = row.get("Start Date")
        if date is None or (hasattr(date, "date") is False):
            continue
        date_str = date.date().isoformat()
        if date_str > end_date or (start_date and date_str < start_date):
            continue
        shares = _safe(row.get("Shares"))
        text = str(row.get("Text") or row.get("Transaction") or "")
        if shares is not None and "sale" in text.lower():
            shares = -abs(shares)
        trades.append(
            InsiderTrade(
                ticker=ticker,
                issuer=None,
                name=row.get("Insider"),
                title=row.get("Position"),
                is_board_director=None,
                transaction_date=date_str,
                transaction_shares=shares,
                transaction_price_per_share=None,
                transaction_value=_safe(row.get("Value")),
                shares_owned_before_transaction=None,
                shares_owned_after_transaction=None,
                security_title=None,
                filing_date=date_str,
            )
        )
        if len(trades) >= limit:
            break

    if not trades:
        return []

    _cache.set_insider_trades(cache_key, [t.model_dump() for t in trades])
    return trades


def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[CompanyNews]:
    cache_key = f"yf_{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    if cached_data := _cache.get_company_news(cache_key):
        return [CompanyNews(**news) for news in cached_data]

    try:
        items = yf.Ticker(ticker).news or []
    except Exception as e:
        logger.warning("yfinance news fetch failed for %s: %s", ticker, e)
        return []

    news_list = []
    for item in items:
        content = item.get("content", item)
        title = content.get("title")
        url = (
            (content.get("canonicalUrl") or {}).get("url")
            or (content.get("clickThroughUrl") or {}).get("url")
            or item.get("link")
        )
        date_raw = content.get("pubDate") or item.get("providerPublishTime")
        if isinstance(date_raw, (int, float)):
            date_str = datetime.datetime.fromtimestamp(date_raw, tz=datetime.timezone.utc).date().isoformat()
        elif isinstance(date_raw, str) and date_raw:
            date_str = date_raw.split("T")[0]
        else:
            date_str = None
        if not title or not url or not date_str:
            continue
        if date_str > end_date or (start_date and date_str < start_date):
            continue
        provider = content.get("provider") or {}
        source = provider.get("displayName") or item.get("publisher") or "Yahoo Finance"
        news_list.append(
            CompanyNews(
                ticker=ticker,
                title=title,
                author=None,
                source=source,
                date=date_str,
                url=url,
                sentiment=None,
            )
        )
        if len(news_list) >= limit:
            break

    if not news_list:
        return []

    _cache.set_company_news(cache_key, [n.model_dump() for n in news_list])
    return news_list


def get_market_cap(ticker: str, end_date: str, api_key: str = None) -> float | None:
    """Market cap in the STATEMENT currency, so it sits next to line items."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    tkr = yf.Ticker(ticker)
    info = _info(tkr)
    if end_date >= today:
        market_cap, _, _ = _market_caps_in_statement_currency(info)
        if market_cap:
            return market_cap

    # Historical (or fallback): price on the date times the CURRENT share
    # count — approximate; buybacks/issuance skew older dates.
    start = (datetime.date.fromisoformat(end_date) - datetime.timedelta(days=14)).isoformat()
    prices = get_prices(ticker, start, end_date)
    shares = _safe(info.get("sharesOutstanding"))
    if not prices or not shares:
        return None
    rate = _fx_rate(info.get("currency") or "USD", info.get("financialCurrency") or info.get("currency") or "USD")
    if rate is None:
        return None
    return prices[-1].close * shares * rate
