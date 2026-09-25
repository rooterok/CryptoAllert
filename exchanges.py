import difflib
import importlib
import logging

logger = logging.getLogger("price-alerts-bot")

# Value type is a ccxt.async_support exchange instance (e.g. ccxt.async_support.binance);
# left untyped since exchange modules are now imported lazily, per exchange id, in get_exchange().
_exchange_cache: dict[str, object] = {}

# Some exchanges split spot and derivatives into separate ccxt exchange classes
# (unlike e.g. bybit/okx, where a single instance's markets already cover both).
# When a symbol isn't found on the "main" id, we also try the linked futures id.
FUTURES_FALLBACK = {
    "binance": "binanceusdm",
}


async def get_exchange(exchange_id: str):
    exchange_id = exchange_id.lower().strip()
    if exchange_id not in _exchange_cache:
        try:
            module = importlib.import_module(f"ccxt.async_support.{exchange_id}")
            klass = getattr(module, exchange_id)
        except (ModuleNotFoundError, AttributeError):
            raise ValueError(f"Биржа '{exchange_id}' не поддерживается ccxt")
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


async def fetch_funding_rate(exchange_id: str, symbol: str) -> float:
    """Current funding rate for a perpetual/swap symbol, as a raw fraction
    (e.g. 0.0001 == 0.01%). Raises whatever ccxt raises if the exchange or
    symbol doesn't support funding rates (e.g. it's a spot market)."""
    exchange = await get_exchange(exchange_id)
    data = await exchange.fetch_funding_rate(symbol)
    rate = data.get("fundingRate")
    if rate is None:
        raise ValueError(f"Биржа не вернула ставку фандинга для {symbol}")
    return rate


async def resolve_funding_symbol(exchange_id: str, symbol: str) -> tuple[str, str, float]:
    """Same idea as resolve_symbol, but validates candidates via the funding-rate
    endpoint instead of the ticker - a symbol only qualifies if it's an actual
    perpetual/swap contract with a funding rate, not just any listed market.

    Returns (actual_exchange_id, actual_symbol, current_funding_rate).
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
            if candidate not in exchange.markets:
                continue
            try:
                rate = await fetch_funding_rate(ex_id, candidate)
            except Exception:
                continue  # e.g. this candidate resolved to a spot market, not a perp
            return ex_id, candidate, rate
        suggestions.extend(_closest_symbols(symbol, exchange.markets))

    msg = f"Не нашёл ставку фандинга для '{symbol}' на '{exchange_id}' (нужен бессрочный контракт)"
    if exchange_id in FUTURES_FALLBACK:
        msg += f" — проверил и спот, и {FUTURES_FALLBACK[exchange_id]}"
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
    except Exception as e:
        logger.info("Batch fetch_tickers failed for %s %s, falling back per-symbol: %s", exchange_id, symbols, e)
    # Fallback: some exchanges don't support batch fetch_tickers with a symbol filter,
    # and/or the batch call above may have returned prices for only some symbols.
    missing = [s for s in symbols if s not in prices]
    for s in missing:
        try:
            t = await exchange.fetch_ticker(s)
            if t.get("last") is not None:
                prices[s] = t["last"]
        except Exception as e:
            logger.warning("Failed to fetch ticker for %s on %s: %s", s, exchange_id, e)
    return prices


async def fetch_funding_rates_for_exchange(exchange_id: str, symbols: list[str]) -> dict[str, float]:
    """Fetch current funding rates for several symbols on one exchange.

    Unlike fetch_prices_for_exchange there's no batch attempt first - ccxt's
    fetchFundingRates support is inconsistent across exchanges (e.g. mexc
    doesn't have it), and funding alerts are checked on a much slower cadence
    than price alerts, so a handful of individual calls is cheap enough.
    """
    prices: dict[str, float] = {}
    for s in symbols:
        try:
            prices[s] = await fetch_funding_rate(exchange_id, s)
        except Exception as e:
            logger.warning("Failed to fetch funding rate for %s on %s: %s", s, exchange_id, e)
    return prices


async def evict_unused(keep_ids: set[str]) -> None:
    """Close and drop cached exchange instances that aren't needed anymore.

    Without this, _exchange_cache only ever grows: any exchange touched even
    once (e.g. just browsing the "choose exchange" keyboard while creating an
    alert, or an alert that was later deleted) stays loaded in memory for the
    life of the process, wasting RAM on market catalogs nobody uses anymore.
    """
    stale = [ex_id for ex_id in _exchange_cache if ex_id not in keep_ids]
    for ex_id in stale:
        exchange = _exchange_cache.pop(ex_id)
        try:
            await exchange.close()
        except Exception:
            pass


async def close_all_exchanges() -> None:
    for ex in _exchange_cache.values():
        try:
            await ex.close()
        except Exception:
            pass
