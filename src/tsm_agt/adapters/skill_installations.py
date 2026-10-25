"""Prompt for preparing a locally installed Skill."""

def installation_prompt(job: dict) -> str:
    return f"""请安装并验证本地 Skill：{job['skill_name']}。
Skill 已保存到：{job['plan_path']}。本次安装记录：{job['id']}。

1. 先用 skill.load 读取这个 Skill 的完整 SKILL.md，再按需读取它引用的安装文档、脚本和资源。根据文档判断是否需要任何前置资源，不要假定每个 Skill 都需要安装环境。
2. 如果需要环境、CLI、插件或其他依赖，先检查现状，复用已有可用资源，只安装或修复缺失项。遇到命令错误先读取报错、定位原因；可恢复错误修复后重试同一步，不能跳过必需步骤或无限重试。
3. 按 Skill 文档执行必要的无业务副作用验证（例如版本、可执行文件、依赖导入或文档规定的自检）。命令必须等待进程退出并检查 exit_code / succeeded；进程启动成功或仍在 RUNNING 不代表安装或验证成功。如果无前置资源，确认文档和引用资源可读取即可。不执行 Skill 的发布、数据修改等业务任务，也不为了验证而运行它们。
4. 需要审批、用户登录或补充信息时，使用正常的审批和澄清流程，明确阻塞原因；未完成的准备不能宣称安装成功。
5. 验证完成后，调用 skill.installation_finish，传入本次 installation_id、status（ready / blocked / failed）、summary 和 checks（实际检查及结果，不能编造）。ready 仅用于必需准备和验证全部通过；不需要前置资源也可报告 ready，并说明理由。最后向用户说明安装位置、是否进行了环境准备、验证结果和剩余阻塞。

只完成本次 Skill 的安装准备。工作区是工具运行上下文；Skill 本身仍位于用户级目录，供其他工作区和会话发现使用。
"""
