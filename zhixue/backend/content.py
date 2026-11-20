from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    text: str
    options: tuple[str, str, str]
    correct: int


@dataclass(frozen=True)
class Objective:
    key: str
    title: str
    hint: str
    explanation: str
    variants: tuple[tuple[Question, Question], tuple[Question, Question]]

    def questions(self, round_number: int) -> tuple[Question, Question]:
        return self.variants[(round_number - 1) % len(self.variants)]


@dataclass(frozen=True)
class Course:
    phase: str
    title: str
    outcome: str
    body: str
    source: tuple[str, str] | None = None
    objectives: tuple[Objective, ...] = ()


SQLITE_COURSES = (
    Course("01 · 建立基础", "理解表、行和列", "用一张学习记录表解释行、列和主键的作用。", """
## 一张表能记下什么

把每一次学习记录放进 `learning_log`。每一行是一条记录，每一列描述这条记录的一个方面。例如 `id` 是记录编号，`topic` 是学习主题，`minutes` 是学习分钟数。

```text
id   topic       minutes
1    SQLite      20
2    英语        15
3    SQLite      35
```

## 区分记录和属性

表里有三条记录和三个字段。查看第 3 行可以了解一次学习。查看 `minutes` 列可以比较三次学习的时长。

## 用主键标识一条记录

同一主题可以学多次，所以 `topic` 会重复。将不重复的记录编号设为主键，可以准确找到第 1 条或第 3 条记录。这个例子使用单列主键。表也可以用多个列共同定义主键。

试一试：如果增加一条「英语，25 分钟」，给它一个新的 `id`，数一数现在有几条记录、几个字段。
""", ("SQLite 官方文档 · CREATE TABLE / PRIMARY KEY", "https://www.sqlite.org/lang_createtable.html#the_primary_key"), (
        Objective("rows-columns", "区分行与列", "看每个值是描述一次学习，还是描述所有记录都拥有的属性。", "一行对应一条学习记录；一列对应所有记录共有的一类属性。", (
            (Question("每增加一条学习记录，表里增加什么？", ("一行", "一列", "一张表"), 0),
             Question("想给所有记录增加‘学习方式’这个属性，应增加什么？", ("一行", "一列", "一个数据库"), 1)),
            (Question("表中哪部分会显示某一次学习的所有属性？", ("整个表", "对应的行", "主题列"), 1),
             Question("比较所有记录的学习时长，应查看哪部分？", ("同一个主题", "最新一行", "时长列"), 2)),
        )),
        Objective("unique-key", "选择可唯一定位的主键", "一个主题可以学多次。想想哪个值在样例里始终不同。", "主键唯一标识记录。示例里的主题和时长都可能重复，独立编号可以区分记录。", (
            (Question("样例中哪个字段适合唯一定位某条学习记录？", ("id", "topic", "minutes"), 0),
             Question("新记录主题已出现过，应如何分配 id？", ("沿用同主题的 id", "分配一个不同的 id", "清空旧记录 id"), 1)),
            (Question("两行的 topic 一样时，仍可用什么区分它们？", ("表名", "不重复的 id", "topic 的列名"), 1),
             Question("主键在这张表里主要解决什么问题？", ("求总时长", "更换列顺序", "精确标识一条记录"), 2)),
        )),
    )),
    Course("02 · 掌握查询", "用 SELECT 筛选记录", "写出查询列并按条件筛选学习记录的语句。", """
## 选择要显示的列

从 `learning_log` 中读取主题与分钟数，可以写：

```sql
SELECT topic, minutes FROM learning_log;
```

`SELECT` 后列出需要返回的列。`FROM` 指定数据来源。

## 只保留符合条件的行

加上 `WHERE`，就能限制返回哪些记录。例如下面只查看至少学了 20 分钟的记录。`>=` 包括恰好 20 分钟。

```sql
SELECT topic, minutes FROM learning_log WHERE minutes >= 20;
```

试一试：只查看 SQLite 学习记录的分钟数。文本值 `'SQLite'` 使用单引号。
""", ("SQLite 官方文档 · SELECT / WHERE", "https://www.sqlite.org/lang_select.html#whereclause"), (
        Objective("select-where", "用 WHERE 限制返回的记录", "先确认需要哪些列，再确定哪些行满足条件。", "SELECT 决定显示哪些列；WHERE 用条件保留符合要求的行。", (
            (Question("要只返回 minutes 列，哪个部分写 minutes？", ("SELECT 后", "FROM 后", "表名中"), 0),
             Question("筛选至少 20 分钟的记录，应使用哪个条件？", ("minutes > 20", "minutes >= 20", "minutes = 20"), 1)),
            (Question("筛选主题为 SQLite，应写哪个条件？", ("topic >= 20", "topic = 'SQLite'", "minutes = 'SQLite'"), 1),
             Question("WHERE 主要影响什么？", ("表名称", "已保存的数据", "返回的记录"), 2)),
        )),
    )),
    Course("03 · 动手应用", "排序并挑出最长的学习记录", "把筛选、排序和返回条数合成一条查询。", """
## 明确排序方向

`ORDER BY minutes DESC` 让时长较长的记录排在前面。使用 `ASC` 则按升序排列。

## 限定返回条数

`LIMIT 1` 将结果限制到一条。先筛选 SQLite 的记录，再按时长从长到短排序。时长相同时按 `id` 升序，便于重复得到相同结果。

```sql
SELECT topic, minutes
FROM learning_log
WHERE topic = 'SQLite'
ORDER BY minutes DESC, id ASC
LIMIT 1;
```

试一试：把查询改成返回最短的两条记录。分别检查排序方向和条数。
""", ("SQLite 官方文档 · ORDER BY / LIMIT", "https://www.sqlite.org/lang_select.html#orderby"), (
        Objective("order-limit", "结合排序与条数限制", "先决定谁应该出现在第一行，再决定结果最多几行。", "用 DESC 把较大的数排在前面，用 LIMIT 限制返回的记录数。", (
            (Question("要把学习时长最高的记录排在前面，应使用什么？", ("ORDER BY minutes DESC", "ORDER BY minutes ASC", "LIMIT 1"), 0),
             Question("至多返回两条记录，应写什么？", ("WHERE minutes = 2", "LIMIT 2", "ORDER BY 2"), 1)),
            (Question("要看用时最短的学习记录，时长列用哪个方向？", ("DESC", "ASC", "LIMIT"), 1),
             Question("时长相等时还想得到稳定顺序，可以再指定什么？", ("另一个表名", "一个更大的 LIMIT", "第二个排序字段"), 2)),
        )),
    )),
)

TEMPLATE_COURSES = (
    Course("01 · 明确范围", "梳理基础概念", "用自己的话说明要学的内容，并列出一个已知和一个待查的问题。", """
## 先确定边界

从你的目标出发，写下三个你已经听过的概念。各写一句自己的理解，在不确定的地方标上问号。

## 找一份能追溯的资料

优先选择教材、官方文档或课程讲义。记录题名、作者和页码，再回到笔记补充定义与例子。
"""),
    Course("02 · 对照例子", "从资料中找出实例", "选一个实例说明关键概念怎样用在具体情境中。", """
## 对照定义与实例

选择一例与你的成果目标有关的材料。先写下它解决的问题，再用前一阶段的概念标出它使用的关键步骤。

## 试着改变一个条件

改动一个输入或限制，先预测结果，再对照原资料检查。记录判断成立所需要的条件。
"""),
    Course("03 · 小步应用", "完成一个可检查的练习", "独立完成一份小成果，并写出如何检验它。", """
## 先写通过条件

把成果拆成一个今天能完成的练习。用两条可观察的条件定义完成，例如给出例子、说明每一步为什么成立。

## 回看证据

完成后检查哪些条件已有证据，哪些仍需验证。把下一次要重做的步骤和资料页码记到笔记中。
"""),
)


def course_at(sample: bool, position: int) -> Course:
    return (SQLITE_COURSES if sample else TEMPLATE_COURSES)[position]


def is_practice(outline: dict | None) -> bool:
    return outline is not None and outline.get("kind") == "practice"


def is_model_lesson(outline: dict | None) -> bool:
    return outline is not None and not is_practice(outline)


def lesson_course(sample: bool, position: int, outline: dict | None) -> Course:
    if outline is None:
        return course_at(sample, position)
    body = f"## 预期成果\n\n{outline['outcome']}\n\n## 实战说明\n\n{outline['instructions']}" if is_practice(outline) else ""
    return Course(phase=outline["phase"], title=outline["title"], outcome=outline["outcome"], body=body)
