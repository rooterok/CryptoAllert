import difflib

import ccxt.async_support as ccxt_async

_exchange_cache: dict[str, "ccxt_async.Exchange"] = {}

# Some exchanges split spot and derivatives into separate ccxt exchange classes
# (unlike e.g. bybit/okx, where a single instance's markets already cover both).
# When a symbol isn't found on the "main" id, we also try the linked futures id.
FUTURES_FALLBACK = {
    "binance": "binanceusdm",
}


async def get_exchange(exchange_id: str):
    exchange_id = exchange_id.lower().strip()
    if exchange_id not in _exchange_cache:
        if not hasattr(ccxt_async, exchange_id):
            raise ValueError(f"Биржа '{exchange_id}' не поддерживается ccxt")
        klass = getattr(ccxt_async, exchange_id)
        instance = klass({"enableRateLimit": True})
        try:
            await instance.load_markets()
        except Exception as e:
            await instance.close()
            raise ValueError(f"Не удалось получить список пар с '{exchange_id}': {e}")
        _exchange_cache[exchange_id] = instance
    return _exchange_cache[exchange_id]


def _symbol_candidates(symbol: str) -> list[str]:
    """BTC/USDT -> also try BTC/USDT:USDT (linear perpetual notation)."""
    candidates = [symbol]
    if "/" in symbol and ":" not in symbol:
        quote = symbol.split("/")[1]
        candidates.append(f"{symbol}:{quote}")
    return candidates


def _closest_symbols(symbol: str, markets: dict, limit: int = 3) -> list[str]:
    base = symbol.split("/")[0] if "/" in symbol else symbol
    same_base = [m for m in markets if m.split("/")[0] == base]
    pool = same_base if same_base else list(markets.keys())
    return difflib.get_close_matches(symbol, pool, n=limit, cutoff=0.4)


async def resolve_symbol(exchange_id: str, symbol: str) -> tuple[str, str, float]:
    """Find `symbol` on `exchange_id`, falling back to its linked futures exchange
    (e.g. binance -> binanceusdm) if it's not on the spot market.

    Returns (actual_exchange_id, actual_symbol, current_price).
    Raises ValueError with a human-readable message (incl. close-match
    suggestions) if the symbol isn't found anywhere.
    """
    symbol = symbol.upper().strip()
    exchange_id = exchange_id.lower().strip()
    tried_ids = [exchange_id]
    if exchange_id in FUTURES_FALLBACK:
        tried_ids.append(FUTURES_FALLBACK[exchange_id])

    suggestions: list[str] = []
    for ex_id in tried_ids:
        try:
            exchange = await get_exchange(ex_id)
        except ValueError:
            continue
        for candidate in _symbol_candidates(symbol):
            if candidate in exchange.markets:
                ticker = await exchange.fetch_ticker(candidate)
                return ex_id, candidate, ticker["last"]
        suggestions.extend(_closest_symbols(symbol, exchange.markets))

    msg = f"Пара '{symbol}' не найдена на '{exchange_id}'"
    if exchange_id in FUTURES_FALLBACK:
        msg += f" (проверил и спот, и {FUTURES_FALLBACK[exchange_id]})"
    if suggestions:
        uniq = list(dict.fromkeys(suggestions))[:3]
        msg += f". Может, имел в виду: {', '.join(uniq)}?"
    raise ValueError(msg)


async def fetch_prices_for_exchange(exchange_id: str, symbols: list[str]) -> dict[str, float]:
    """Fetch current prices for several symbols on one exchange, batching where possible."""
    exchange = await get_exchange(exchange_id)
    prices: dict[str, float] = {}
    try:
        tickers = await exchange.fetch_tickers(symbols)
        for s, t in tickers.items():
            if t.get("last") is not None:
                prices[s] = t["last"]
        return prices
    except Exception:
        pass
    # Fallback: some exchanges don't support batch fetch_tickers with a symbol filter
    for s in symbols:
        try:
            t = await exchange.fetch_ticker(s)
            if t.get("last") is not None:
                prices[s] = t["last"]
        except Exception:
            continue
    return prices


async def close_all_exchanges() -> None:
    for ex in _exchange_cache.values():
        try:
            await ex.close()
        except Exception:
            pass
