"""消费者适配层。

**这一层是 core 与具体 app 之间唯一的翻译处。**

core 里不许出现任何 app 的专有词汇（`concept` / `blocks` / `recipe` …），
由 `tests/test_independence.py` 机械强制。所以：

* core 只知道"目标 id"、"字段路径"、"槽位语义"
* 把"内容目录里那些 YAML 文件的第 N 条记录"翻译成 core 认识的东西，是本包的职责

新增一个 app 只需要在这里加一个模块，**`core/` 与 `sources/` 一行不用动**
（这正是 IND-4 / A-C13 要证明的事）。
"""
