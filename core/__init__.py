"""core — 共享核心（无调用面耦合）。

lakejob 的共享核心层。core 不知道有 web / cli / script 适配器的存在；
所有调用面只依赖 core，core 不反向依赖任何适配器。
"""

__version__ = "0.1.0"
