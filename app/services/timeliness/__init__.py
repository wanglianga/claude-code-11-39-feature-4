"""退税审核到账时效与状态可信度分析服务。

只产出时效分析事实与数据质量证书：
不预测现金、不分配银行流水、不关闭申报行，也不代办任何申报/退税/付款操作。
"""

from .compiler import build_run
from .coverage import (
    apply_coverage_correction,
    register_coverage_proof,
)
from .events import register_revocation_notice

__all__ = [
    "build_run",
    "register_coverage_proof",
    "apply_coverage_correction",
    "register_revocation_notice",
]
