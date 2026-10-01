"""行业层Agent包：A13科技/A14消费/A15周期/A16医药 + A20兜底行业。

A20（`GenericIndustryAgent`）与前四者的区别：行业名与关注指标**按每次请求解析**
（本地名录 `quant_stock_basic.industry`），因此**一个单例服务任意行业**，
不需要为每个行业各建一个类。
"""

from src.domain.agents.industry.consumer import ConsumerIndustryAgent
from src.domain.agents.industry.cyclical import CyclicalIndustryAgent
from src.domain.agents.industry.generic import GenericIndustryAgent
from src.domain.agents.industry.pharma import PharmaIndustryAgent
from src.domain.agents.industry.tech import TechIndustryAgent

__all__ = [
    "TechIndustryAgent",
    "ConsumerIndustryAgent",
    "CyclicalIndustryAgent",
    "PharmaIndustryAgent",
    "GenericIndustryAgent",
]
