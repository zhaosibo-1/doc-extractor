"""文档 / 表格 / 图像结构化抽取工具链。

把任意格式的文件（PDF / DOCX / XLSX / CSV / TXT / MD / 图片）变成一份
**带来源定位、带置信度、经过校验**的结构化 JSON。

三层分工：
    解析层 loaders/  —— 把任意格式转成统一的中间表示（Document / Block / TableData）
    抽取层 extractor —— Schema 驱动地填字段（规则优先，模型补漏）
    校验层 validators —— 类型强转、正则、枚举、跨字段一致性
"""

__version__ = "1.0.0"
