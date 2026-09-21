import re
from pathlib import Path

def analyze_ea(path):
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    low = text.lower()

    def count_terms(terms):
        return {t: len(re.findall(r"\b"+re.escape(t.lower())+r"\b", low)) for t in terms if re.search(r"\b"+re.escape(t.lower())+r"\b", low)}

    indicators = count_terms(["iMA","iADX","iATR","iRSI","iMACD","iBands","iStochastic","iCCI"])
    order_terms = count_terms(["Buy","Sell","BuyStop","SellStop","BuyLimit","SellLimit"])
    exits = count_terms(["StopLoss","TakeProfit","PositionClose","PositionClosePartial","Trailing","break-even","breakeven"])

    functions = sorted(set(re.findall(r"\b(OnInit|OnTick|OnTimer|OnTrade|OnTradeTransaction|OnDeinit)\s*\(", text, re.I)))
    has_buy = bool(re.search(r"\b(CTrade\s*::\s*Buy|trade\.Buy|OrderSend\s*\()", text, re.I))
    has_sell = bool(re.search(r"\b(CTrade\s*::\s*Sell|trade\.Sell|OrderSend\s*\()", text, re.I))

    return {
        "source":"EA",
        "file":Path(path).name,
        "lines":text.count("\n")+1,
        "event_handlers":functions,
        "indicators":indicators,
        "order_terms":order_terms,
        "exit_terms":exits,
        "has_buy_logic":has_buy,
        "has_sell_logic":has_sell,
        "note":"EA parsing describes documented strategy logic signals; it does not infer undocumented intent."
    }
