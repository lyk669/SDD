# 智能日报生成器 — 任务清单

## 元信息

- 关联规范：specs/proposal.md, specs/design.md
- 任务总数：8（原群消息 / 群推送任务已取消）
- 预计总执行时间：3-4 小时（含测试）
- 执行策略：按依赖关系顺序执行，独立任务可并行

---

## Task 1: 创建项目结构和基础配置

**描述**：初始化项目目录结构，创建 pyproject.toml，安装核心依赖，创建配置文件模板。

**输出**：项目根目录结构 + pyproject.toml + config.yaml.example
**依赖**：无

**验收标准**：
- [ ] 目录结构符合 design.md §2（collector/, generator/, notifier/, shared/, tests/）
- [ ] pyproject.toml 包含 httpx, pyyaml, jinja2 依赖
- [ ] `uv sync` 成功安装所有依赖
- [ ] config.yaml.example 包含 members 映射表模板

---

## Task 2: 实现共享基础层

**输出**：shared/config.py, shared/logger.py, shared/errors.py, shared/storage.py
**依赖**：Task 1

**验收标准**：
- [ ] config.py 能读取 config.yaml 并返回配置对象
- [ ] config.py 在配置缺少必填字段时抛出明确的错误信息
- [ ] logger.py 输出 JSON lines 格式日志
- [ ] errors.py 定义 CollectorError, GeneratorError, NotifierError
- [ ] storage.py 能创建 SQLite 数据库，写入和查询日报记录
- [ ] 单元测试通过

---

## Task 3: 实现 GitHub 采集模块

**输出**：collector/github.py + tests/test_github.py
**依赖**：Task 2

**验收标准**：
- [ ] collect() 返回 CollectResult[CommitRecord]（ADR-003）
- [ ] CommitRecord 含全部 7 个字段
- [ ] 支持分页；超时重试 3 次；限流等待后重试
- [ ] 多仓库时单仓失败不丢弃其他仓已采数据
- [ ] mock 单元测试通过

---

## Task 4: 实现飞书任务采集模块

**输出**：collector/lark_task.py + tests/test_lark_task.py
**依赖**：Task 2

**验收标准**：
- [ ] collect() 返回 CollectResult[TaskRecord]（ADR-003）
- [ ] TaskRecord 含全部 5 个字段
- [ ] token 过期自动刷新；超时重试 3 次
- [ ] mock 单元测试通过

---

## Task 5: ~~飞书消息采集~~（已取消）

本期不做飞书群消息采集。

---

## Task 6: 实现日报生成模块

**描述**：按「代码提交 → 任务进展」组织日报，输出 Markdown 和 HTML。

**输出**：generator/formatter.py, generator/template.py + tests/test_generator.py
**依赖**：Task 2

**验收标准**：
- [ ] generate() 签名符合 design.md §4.2
- [ ] markdown 含「代码提交」「任务进展」两节
- [ ] 无记录显示「今日无记录」；采集失败显示「数据获取失败」
- [ ] HTML 可渲染；单元测试通过

---

## Task 7: 实现邮件推送模块

**输出**：notifier/email.py + tests/test_email.py
**依赖**：Task 6

**验收标准**：
- [ ] send(report, recipients) 契约满足
- [ ] 主题含日期和团队名；正文为 HTML
- [ ] 失败重试 2 次；mock SMTP 测试通过

---

## Task 8: ~~飞书群推送~~（已取消）

本期不做飞书群机器人 / Webhook 推送。

---

## Task 9: 实现主编排入口

**输出**：main.py
**依赖**：Task 3, Task 4, Task 6, Task 7

**验收标准**：
- [ ] `python main.py` 完整流程
- [ ] `--check` / `--dry-run` 可用
- [ ] 单源失败不影响其他源；全失败不生成空日报
- [ ] 日志含开始时间、采集条数、推送结果、结束时间

---

## Task 10: 集成测试

**输出**：tests/test_integration.py
**依赖**：Task 9

**验收标准**：
- [ ] 正常 / 降级 / 空数据 / 全部失败场景
- [ ] mock 环境下执行耗时 < 60 秒

---

## 执行顺序

| 阶段 | 任务 | 说明 |
|------|------|------|
| 1 | T1 | 项目初始化 |
| 2 | T2 | 共享基础层 |
| 3 | T3 + T4 | 采集模块可并行 |
| 4 | T6 | 日报生成 |
| 5 | T7 | 邮件推送 |
| 6 | T9 | 主编排 |
| 7 | T10 | 集成测试 |

---

*规范版本：v1.1 | 已取消飞书群消息 / 群推送任务*
