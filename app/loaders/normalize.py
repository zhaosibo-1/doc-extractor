"""IR 归一化：所有加载器跑完之后，统一做一遍「跨格式应该一致」的整理。

为什么需要这个模块
------------------

统一中间表示（IR）的价值，全在于「抽取层不用关心文档从哪来」。但这条承诺
很容易被悄悄破坏：只要某个格式的加载器少标了一个 ``Block.kind``，
抽取层对应的策略就会在那个格式上**静默失效**。

这个模块就是被这样一个真实缺陷逼出来的：
``text_loader`` 会把「发票号码：12345」这样的行标成 ``kind="kv"``，
而 ``docx_loader`` 和 ``pdf_loader`` 都只标 ``"paragraph"``。
抽取层里置信度最高的一路（标签行匹配，0.92）只认 ``kv``，
于是同一份合同内容 —— 存成 ``.txt`` 能抽到 100%，存成 ``.docx`` 是 0%。
两份文件的字节不同，但文本完全一样，结果却天差地别。

根因不是「忘了在 docx 里加一行标记」，而是**判定逻辑长在了某一个格式加载器里**。
所以这里把它提升为共享的归一化步骤：加载器只管把格式解成块，
「这块文字是不是键值行」由 IR 层统一决定。这样新增格式时，
不可能再出现「新加载器漏标 kind」这类问题。
"""

from __future__ import annotations

from ..ir import Document
from ..textutil import collapse_ws, split_label_value

#: 参与键值行判定的块类型。
#: 只处理纯文本块 —— ``heading`` 是加载器依据真实样式（Word 标题、Markdown ``#``）
#: 做出的判断，比「有没有冒号」更可靠，不该被这里覆盖。
_TEXT_KINDS = frozenset({"paragraph", "list_item"})


def normalize_document(doc: Document) -> Document:
    """对文档做一遍与格式无关的整理，就地修改并返回。

    目前只做一件事：**把「标签: 值」的普通段落提升为 ``kv`` 块**。
    判定顺序与 ``text_loader`` 保持一致 —— 先认带值的键值行，
    再认只有标签没值的行（``任职要求：`` 后面接一段内容，值在下一块）。
    """
    for block in doc.blocks:
        if block.kind not in _TEXT_KINDS:
            continue
        text = collapse_ws(block.text)
        if not text:
            continue
        label, _value = split_label_value(text)
        if label:
            block.kind = "kv"
            block.text = text
    return doc


__all__ = ["normalize_document"]
