from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    design: str
    parent: str | None = None


STEPS = (
    Step("setup", "启动隔离环境", "通过正式启动入口初始化临时数据库与知识库，并配置本机模拟模型。"),
    Step("library-search", "知识库检索定位", "以单字、双字和混排短语检索临时资料，核对摘要、原文位置和版本。"),
    Step("library-reindex", "外部修改与索引重建", "改动隔离原件，核对旧命中消失、新位置出现和重建后原件字节。", "library-search"),
    Step("library-anchor", "拒绝失效原文位置", "验证旧版本与不存在的行分别得到明确错误，并能直接打开当前文件。", "library-search"),
    Step("ocr", "单页识别授权与范围", "实跑本机 OCR，再用独立授权发送合成 PDF 的所选页图，核对接收内容和未确认草稿。"),
    Step("ocr-conflict", "校对冲突与索引", "保留先保存的校对和原件，拒绝旧版本及撤销后的外部请求。", "ocr"),
    Step("goal", "提交学习目标", "从目标对话整理目标卡，确认主模型发送范围后提交，重复提交沿用同一项目。"),
    Step("route", "生成学习路线", "后台 Worker 生成课程顺序与目标，路线独立于课程正文保存。"),
    Step("retry", "请求失败与重试", "模拟生成接口暂不可用，观察 Worker 重试及账本。", "route"),
    Step("draft", "生成待核验草稿", "草稿留在生成队列，核验前课程正文不可读。"),
    Step("delete", "删除准备任务", "在模型调用进行中确认删除，检查迟到草稿与路线。", "draft"),
    Step("verify", "后台独立核验", "复核模型只收到单独授权的资料定位，依据核验结果发布支持片段。"),
    Step("pause", "暂停并保留核验结果", "核验请求发出后停止任务，待返回的核验结果保存。", "verify"),
    Step("resume", "恢复已暂停任务", "继续准备后使用已经保存的结果发布课程。", "verify"),
    Step("dispute", "争议与依据不足", "检查争议、依赖争议和无效引用是否留在发布正文之外。", "verify"),
    Step("read", "阅读、记笔记与计时", "保存当前课程笔记并用学习计时记录练习。"),
    Step("ask", "课内答疑", "单独确认答疑范围，验证问题和带出处的答复。"),
    Step("save", "标为已学并保存", "保存已学日期，后台整理获得授权的答疑，独立核验后更新课程版本。"),
    Step("review", "生成复习题与独立评价", "从已核验依据生成复习题，到期后提交，等待评阅 Worker。"),
    Step("schedule", "安排下次复习", "根据独立评价结果变更阶段并更新计划，仍能查看原答案。"),
    Step("schedule-delay", "延迟评分与单轮待办", "模拟逾期作答和晚到评分，核对实际提交日、真实到期日和唯一待办。", "schedule"),
    Step("restart", "关闭与重启恢复", "重新启动同一隔离工作区，检查课程、笔记、复习和数据库。"),
    Step("restart-unsent", "重启恢复未发送预留", "终止持有未发送草稿预留的 Worker，用原额度续写同一知识点。", "restart"),
    Step("restart-sent", "重启核对已发送请求", "在回环接口收到草稿请求后中断，核对未知费用和人工继续。", "restart"),
    Step("conflict", "文件版本冲突", "外部更改笔记后尝试保存旧稿，由用户预览并合并两个版本。"),
    Step("backup", "加密备份与恢复", "用临时口令创建加密归档，预览并恢复资料以及整库。"),
    Step("revision-update", "来源变更与课程修订", "实际修改合成资料，检查今天的待办和原引文对照。"),
    Step("revision-preview", "拒绝过期发送预览", "预览所选资料后再改原件，验证旧确认被拒绝且模型未收到请求。", "revision-update"),
    Step("revision-keep", "核验后选择保留旧版", "完成一次独立核验，选择保留旧版，检查正式正文与来源待办。", "revision-update"),
    Step("revision-accept", "确认接受课程新版", "再次完成独立核验，查看受影响的摘录，确认后发布并核对旧作答和排期。", "revision-update"),
    Step("revision-seen", "单独确认已知悉新版", "确认新版产生查看待办，标为已知悉后清除提醒。", "revision-update"),
    Step("revision-anchor", "重新关联原笔记摘录", "旧链接待关联时预览两处文字，确认新版位置并核对笔记原文。", "revision-update"),
    Step("revision-restore", "恢复已确认的关联", "备份已更新课程与关联，实际改动关联后恢复，核对原链接和无重复模型发送。", "revision-update"),
    Step("revision-restart", "重启后定位与历史", "重启正式服务，检查已关联的原链接、旧版、已学与复习记录及数据库。", "revision-update"),
    Step("budget", "模型金额额度", "在独立工作区设置合成连接单价及日限额和任务限额。"),
    Step("budget-task", "单任务额度暂停", "检查已保存的路线、未发送的草稿和已落账的估算费用。", "budget"),
    Step("budget-resume", "调高额度并继续原任务", "检查调高后仍等待确认，继续时只发送原课程的草稿。", "budget"),
    Step("budget-daily", "每日额度拦截", "额度不足的同日新任务在发送前暂停。", "budget"),
    Step("budget-rollover", "北京时间跨日继续", "检查默认提醒及手动继续，开启自动继续后验证其作用范围。", "budget"),
    Step("budget-restore", "恢复备份并核对用量", "从较早归档恢复工作区，保留后续支出，恢复的任务等待确认。", "budget"),
    Step("route-updates", "目标与路线调整", "启动独立本地项目，保存原课、笔记、已学日期与复习排期。"),
    Step("route-goal-preview", "预览目标与日期", "对照原值与新值，检验更改预览内容后的确认被拒绝。", "route-updates"),
    Step("route-goal-confirm", "确认新目标与日期", "确认原预览并检查重复确认只保留一份新版本。", "route-updates"),
    Step("route-edit-preview", "预览路线、标记与实战", "检查知识点顺序、待巩固标记和实战位置，验证旧版本预览被拒绝。", "route-updates"),
    Step("route-practice-conflict", "确认路线和实战文件冲突", "确认路线后观察真实 Worker 的同名文件错误，并检查原文件。", "route-updates"),
    Step("route-practice-retry", "重试后读取实战", "通过正式重试接口生成 Markdown，核对知识库登记和已学状态。", "route-updates"),
    Step("route-edit-restart", "重启后读回路线", "检查重启后的历史、实战、个人标记、原笔记和复习排期。", "route-updates"),
    Step("route-assistant-known", "课内指令标记已会", "发送课内文字指令，预览并确认个人标记，核对原已学状态和复习排期。", "route-updates"),
    Step("route-assistant-earlier", "课内指令提前课程", "确认将一节课程向前移动一位，核对其余课程及实战的相对顺序。", "route-updates"),
    Step("route-assistant-goal", "课内指令修改目标", "从关联课程请求目标编辑，预览并确认预期成果，检查原日期和路线。", "route-updates"),
    Step("route-group-preview", "预览阶段与分支重组", "旧测评已有答题时预览新旧范围，验证修改预览内容后的确认被拒绝。", "route-updates"),
    Step("route-group-confirm", "确认重组并生成阶段测评", "保留旧测评，两节必学课完成后生成新测评。保持选做实战未学，改必学课分支后沿用同一测评。", "route-updates"),
    Step("route-group-restart", "重启后读回阶段与测评", "从同一工作区读回路线归属、两套测评、原笔记、实战和复习排期。", "route-updates"),
    Step("real-model", "真实模型样本联调", "确认模型范围和可能的费用后，启动隔离的样本工作区。"),
    Step("real-route", "提交样本目标与路线", "正式 Worker 用主模型生成路线。", "real-model"),
    Step("real-draft", "生成首课草稿", "保留实际生成的小节供复核。", "real-model"),
    Step("real-verify", "真实模型独立复核", "复核草稿与选定的公开资料摘要，记录结论和引用。", "real-model"),
    Step("real-publish", "发布可读样本", "检查实际正文与核验版本，保存供人工抽查。", "real-model"),
    Step("real-reading", "阅读与笔记", "从真实发布版本读取正文并保存临时笔记。", "real-model"),
    Step("real-answer", "发送样本答疑", "主模型依据已核验材料回答样本问题。", "real-model"),
    Step("real-save", "标为已学并整理", "尝试生成并独立复核答疑补充，保存已学日期。", "real-model"),
    Step("real-review", "到期复习与评阅", "生成复习题，提交样本答案并记录实际评价。", "real-model"),
    Step("real-schedule", "复习结算与排期", "根据实际评价检查后续排期和持久结果。", "real-model"),
    Step("real-usage", "模型用量记录", "保留调用数、用量、账本费用及费用未报告的调用。", "real-model"),
)
BY_ID = {step.id: step for step in STEPS}
STATE_LABELS = {
    "running": "执行中", "passed": "通过", "failed": "失败", "interrupted": "已中断",
    "unverified": "未验证",
}


class CheckFailed(RuntimeError):
    """A verification failure with a fixed message that can go into a report."""


def check(condition, reason: str) -> None:
    if not condition:
        raise CheckFailed(reason)
