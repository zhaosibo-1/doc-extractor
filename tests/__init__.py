"""测试包。

让 ``tests`` 成为一个真正的包（而不是靠 pytest 的 sys.path 注入），
这样测试之间可以共享 ``helpers``，并且从任何目录运行 pytest 都能导入 ``app``。
"""
