from dataclasses import dataclass


@dataclass(frozen=True)
class StageQuestion:
    prompt: str
    options: tuple[str, str, str]
    correct: int
    explanation: str


SQLITE_STAGE_QUESTIONS = (
    (
        (
            StageQuestion("日志中有两次学习，主题都是「SQLite」。新增第二次学习时应该怎样保存？",
                          ("新建一行并使用不同的记录编号", "给第一行增加一个主题列", "覆盖第一次学习的记录"), 0,
                          "每次学习对应独立的一行。主题可以重复，主键必须区分两行。"),
            StageQuestion("每次学习都要记录使用的资料。已有记录应如何表示这个新属性？",
                          ("每份资料独占一张日志表", "增加一个资料列，旧记录可暂时没有值", "在表尾统一添加一行资料"), 1,
                          "资料是每次学习的一个属性，因此用列表示。"),
        ),
        (
            StageQuestion("两位同学都记了 20 分钟。需要修改其中一次的时长时，哪个查找条件最稳妥？",
                          ("时长等于 20", "该次学习的唯一编号", "两位同学都在用的主题"), 1,
                          "用唯一编号定位那一行可以避免误改其他记录。"),
            StageQuestion("把学习表交给新同学时，怎样解释其中的一行？",
                          ("本项目所有学习的主题", "所有记录共有的一种属性", "一次完整的学习记录"), 2,
                          "一行汇集一次学习的多个属性，例如时长、主题和编号。"),
        ),
    ),
    (
        (
            StageQuestion("只要展示学习时长，并且仅考虑 SQLite 的记录，应选哪条查询？",
                          ("SELECT topic FROM learning_log WHERE minutes > 0",
                           "SELECT minutes FROM learning_log WHERE topic = 'SQLite'",
                           "SELECT topic, minutes FROM learning_log"), 1,
                          "SELECT 只需要列出 minutes，WHERE 用主题值筛选行。"),
            StageQuestion("WHERE minutes >= 20 应返回下列哪些记录？",
                          ("20 分钟和 35 分钟", "只返回 35 分钟", "15 分钟和 20 分钟"), 0,
                          "大于等于 20 包含 20 本身，以及比 20 更大的时长。"),
        ),
        (
            StageQuestion("报表要显示主题和时长，但排除 10 分钟及以下的记录。如何组合查询？",
                          ("SELECT minutes FROM learning_log WHERE minutes >= 10",
                           "SELECT topic FROM learning_log WHERE minutes > 10",
                           "SELECT topic, minutes FROM learning_log WHERE minutes > 10"), 2,
                          "需要选择两列，且使用严格大于来排除等于 10 的记录。"),
            StageQuestion("一条记录存了主题 SQLite 和时长 15。哪个条件会保留该记录？",
                          ("WHERE topic = 'SQLite' AND minutes >= 20",
                           "WHERE topic = 'SQLite' AND minutes < 20",
                           "WHERE topic = '英语'"), 1,
                          "主题等于 SQLite 且时长 15 小于 20，所以两部分条件都成立。"),
        ),
    ),
    (
        (
            StageQuestion("只返回用时最长的两次 SQLite 学习，且同分时按编号升序。如何写查询？",
                          ("SELECT id FROM learning_log WHERE topic = 'SQLite' ORDER BY minutes DESC, id ASC LIMIT 2",
                           "SELECT id FROM learning_log WHERE topic = 'SQLite' ORDER BY minutes ASC LIMIT 2",
                           "SELECT id FROM learning_log ORDER BY id ASC LIMIT 2"), 0,
                          "先筛选主题，再按时长降序和编号升序排序，最后用 LIMIT 2 取两行。"),
            StageQuestion("SQLite 学习记录有 20、20 和 35 分钟三条，最短一条的结果应如何保持一致？",
                          ("只使用 LIMIT 1", "ORDER BY minutes ASC, id ASC LIMIT 1", "ORDER BY minutes DESC LIMIT 1"), 1,
                          "时长升序先找到最短记录，编号升序给相同时长固定顺序。"),
        ),
        (
            StageQuestion("计划找出英语学习中用时最短的三条记录。下面哪个组合满足条件？",
                          ("WHERE topic = '英语' ORDER BY minutes DESC LIMIT 3",
                           "WHERE topic = '英语' LIMIT 3",
                           "WHERE topic = '英语' ORDER BY minutes ASC, id ASC LIMIT 3"), 2,
                          "WHERE 确定主题，时长升序把短记录排在前面，LIMIT 3 限制条数。"),
            StageQuestion("已经按 minutes DESC 和 id ASC 排好序。结果最多显示一条需要补哪段？",
                          ("ORDER BY id DESC", "WHERE minutes = 1", "LIMIT 1"), 2,
                          "LIMIT 1 取已经排序的第一行。minutes = 1 是筛选时长等于一的行。"),
        ),
    ),
)
