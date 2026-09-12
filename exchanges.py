import ccxt.async_support as ccxt_async

_exchange_cache: dict[str, "ccxt_async.Exchange"] = {}


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


async def validate_symbol_and_get_price(exchange_id: str, symbol: str) -> float:
    """Raises ValueError with a human-readable message if the symbol doesn't exist."""
    exchange = await get_exchange(exchange_id)
    symbol = symbol.upper().strip()
    if symbol not in exchange.markets:
        raise ValueError(f"Пара '{symbol}' не найдена на бирже '{exchange_id}'")
    ticker = await exchange.fetch_ticker(symbol)
    return ticker["last"]


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
