"""TradeThrone AI Quant Lab.

Natural-language strategy parsing and the AI Strategy Doctor
(robustness scoring) live here.
"""

from app.quant.nl_parser import parse_strategy_text
from app.quant.doctor import diagnose_strategy

__all__ = ["parse_strategy_text", "diagnose_strategy"]