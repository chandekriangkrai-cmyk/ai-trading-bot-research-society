MAJOR_FX_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
)


def normalize_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace("/", "")


def is_major_fx(symbol: str) -> bool:
    return normalize_symbol(symbol) in MAJOR_FX_SYMBOLS
