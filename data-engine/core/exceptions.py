"""
全局异常处理模块，统一定义各个领域算子抛出的异常。
"""

class QuicOperatorException(Exception):
    """
    所有算子库异常的根类 (大驼峰命名)
    """
    pass

class VideoCorruptedException(QuicOperatorException):
    """
    视频数据损坏或无法解码异常
    """
    pass

class ModelWeightNotFoundException(QuicOperatorException):
    """
    模型权重文件缺失异常，通常在模型懒加载时抛出
    """
    pass

class SchemaValidationException(QuicOperatorException):
    """
    QRDF 数据 Schema 校验未通过异常
    """
    pass

class ExporterException(QuicOperatorException):
    """
    导出分片时发生的异常
    """
    pass

class CapabilityNotFoundException(QuicOperatorException):
    """
    SDK 能力声明中找不到指定 operator_id
    """
    pass

class CapabilityInvokeException(QuicOperatorException):
    """
    统一 invoke 调用失败
    """
    pass
