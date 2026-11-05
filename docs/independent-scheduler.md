# 独立云端定时触发

GitHub 内置 schedule 可能延迟或漏触发。新闻仍在 GitHub Actions 执行，使用
cron-job.org 独立触发 workflow_dispatch，并保留 GitHub 原有定时作为备用。

- 北京时间每天 08:00、11:00、17:00 开始执行新闻任务，分析和发送还需要运行时间。
- 每个两小时窗口内，在第 0、10、30、50 分钟再次触发；已发送的时段直接跳过。
- 发送台账只包含日期和时段，通过 Actions Artifact 保存 90 天，独立于新闻缓存。
- 未记录成功发送的任务会标记失败，允许后续触发重试。
- 每天 07:00 签到，07:20、07:40 补触发。
- 任一外部服务或 Actions Runner 故障仍可能造成延迟，这不是准点送达的保证。

## 一次性接入

1. 在 https://console.cron-job.org/signup 注册、验证邮箱并登录。
2. 在 cron-job.org 的 Settings 中创建 API Key；保存到本仓库 Actions Secret
   `CRON_JOB_API_KEY`。不要把密钥发到聊天、提交到代码或写入公开日志。
3. 在 GitHub 创建独立 Fine-grained personal access token：Resource owner 为
   `nemowori`，Repository access 只选择 `TrendRadar`，Repository permissions
   只增加 **Actions: Read and write**。Metadata read 是 GitHub 必选项。
   不授予 Contents、Secrets、Administration 等写权限。
4. 把这个专用令牌保存到本仓库 Actions Secret `SCHEDULER_GITHUB_TOKEN`。
   令牌会作为请求头保存在 cron-job.org，用来触发本仓库工作流。
   GitHub 的 Actions write 同时允许管理 Actions，并非“仅触发”权限。
   根据所选有效期，在到期前更换此令牌并重新运行配置工作流。
5. 手动运行 **Configure Independent Scheduler**。它先执行不发消息、不调用
   AI 的鉴权检查，再创建或更新两个定时任务，不会创建同名重复任务。
6. 在 cron-job.org 的执行记录和 GitHub Actions 中核对第一次自动运行。

`DINGTALK_WEBHOOK_URL`、`AI_API_KEY` 留在 GitHub，不交给独立定时服务。

## 验证和停用

`Get Hot News` 的 `check_only=true` 只检查配置、恢复发送台账，不调用 AI 或
发送消息。自动触发应使用 `send_now=false`、`check_only=false`，保留防重复。
`send_now=true` 是用户明确要求时的强制预览，会绕过常规定时。

停止独立触发时，在 cron-job.org 禁用本次创建的两项任务即可；不要删除
GitHub Actions 历史、发送台账或其他用户任务。
