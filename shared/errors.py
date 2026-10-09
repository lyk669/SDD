"""自定义异常类型。"""


class DailyReportError(Exception):
    """智能日报生成器基础异常。"""


class ConfigError(DailyReportError):
    """配置缺失或无效。"""


class CollectorError(DailyReportError):
    """采集层错误。"""


class GeneratorError(DailyReportError):
    """生成层错误。"""


class NotifierError(DailyReportError):
    """推送层错误。"""
