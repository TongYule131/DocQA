"""在线评估的共享请求上限和数据目录保护；计数包括索引构建。"""
from pathlib import Path


class RequestBudget:
    def __init__(self, limit: int):
        if type(limit) is not int or limit < 1:
            raise ValueError('请求上限必须为正整数')
        self.limit, self.used = limit, 0

    def take(self):
        # 在每次外部请求之前扣除额度，失败的请求也计数，绝不自动重试。
        if self.used >= self.limit:
            raise RuntimeError('已达到在线评估请求上限')
        self.used += 1


def validate_online_dir(path: Path) -> Path:
    from app.config import Settings
    target = path.resolve()
    allowed = Path(__file__).resolve().parent.parent / 'data'
    formal = Settings.from_env().data_dir.resolve()
    if not target.is_relative_to(allowed) or target == allowed or target == formal or formal.is_relative_to(target):
        raise ValueError('在线评估目录必须是项目 data 下的独立子目录，且不能覆盖正式数据目录')
    return target
