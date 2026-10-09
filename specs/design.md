# 智能日报生成器 — 架构设计

## 1. 系统架构

### 1.1 架构模式

采用**管道式架构**（Pipeline Architecture）：数据从外部数据源流入，经过采集、生成、推送三个阶段，最终到达目标用户。

选择管道式架构的理由：
- 数据流向单一、明确（输入 → 处理 → 输出）
- 各阶段职责独立，便于单独测试和替换
- 系统规模小（5 人团队），不需要微服务的复杂性
- 运行方式为定时任务，不需要事件驱动的实时性

### 1.2 架构图

```
外部数据源                  系统内部                        输出目标
─────────                ──────────                    ────────

GitHub API ─┐
            ├→ [采集层] → 原始数据 → [生成层] → DailyReport
飞书任务 API ┘  Collector              Generator     │
                                                    ↓
                                              [推送层] Notifier
                                                    ↓
                                                  邮件

                    [共享基础层]
                    配置管理 │ 日志 │ 错误处理 │ 数据存储(SQLite)
```

## 2. 模块职责

### 2.1 采集层（collector/）

| 文件 | 职责 | 不负责 |
|------|------|--------|
| github.py | 从 GitHub API 获取 commit 记录 | 数据格式化、去重判断 |
| lark_task.py | 从飞书开放平台获取任务状态变更 | 推送通知、群消息采集 |

### 2.2 生成层（generator/）

| 文件 | 职责 | 不负责 |
|------|------|--------|
| formatter.py | 将原始数据整理为 MemberReport，生成 Markdown 和 HTML | 数据采集、API 调用 |
| template.py | 管理日报模板（Markdown/HTML），支持自定义 | 推送通知 |

### 2.3 推送层（notifier/）

| 文件 | 职责 | 不负责 |
|------|------|--------|
| email.py | 通过 SMTP 发送 HTML 格式日报邮件 | 数据采集、日报生成 |

### 2.4 共享基础层（shared/）

| 文件 | 职责 |
|------|------|
| config.py | 读取 config.yaml，校验配置完整性，提供全局配置访问 |
| logger.py | 统一日志格式（JSON lines），日志级别管理 |
| errors.py | 自定义异常类型，错误处理策略实现 |
| storage.py | SQLite 操作封装，日报历史存储和查询 |

### 2.5 编排入口（main.py）

- 按顺序调用：采集 → 聚合 → 生成 → 推送
- 处理全局异常，记录执行状态
- 支持 `--check` 模式（健康检查）和 `--dry-run` 模式（不推送）

## 3. 数据模型

### 3.1 采集层输出

```python
CommitRecord:
    author: str
    message: str
    timestamp: datetime
    repo: str
    additions: int
    deletions: int
    files_changed: int

TaskRecord:
    assignee: str
    title: str
    status_from: str
    status_to: str
    updated_at: datetime

CollectResult[T]:
    success: bool
    records: list[T]
    error_message: str | None
```

### 3.2 生成层输入/输出

```python
MemberReport:
    name: str
    github_username: str
    commits: list[CommitRecord]
    tasks: list[TaskRecord]
    commits_failed: bool
    tasks_failed: bool

DailyReport:
    date: date
    team_name: str
    members: list[MemberReport]
    generated_at: datetime
    markdown: str
    html: str
```

### 3.3 身份映射

```yaml
members:
  - name: "张三"
    github: "zhangsan"
    lark: "zhangsan@company.com"
    # lark_open_id: "ou_xxx"   # 可选
```

## 4. 接口契约

### 4.1 采集层接口

```python
# github.py
def collect(repos: list[str], since: datetime, until: datetime) -> CollectResult[CommitRecord]

# lark_task.py
def collect(tasklist_id: str, since: datetime, until: datetime) -> CollectResult[TaskRecord]
```

### 4.2 生成层接口

```python
# formatter.py
def generate(members: list[MemberReport], date: date, team_name: str) -> DailyReport
```

### 4.3 推送层接口

```python
# email.py
def send(report: DailyReport, recipients: list[str]) -> bool
```

## 5. 技术选型（ADR）

### ADR-001：使用 httpx 作为 HTTP 客户端

**状态**：已采纳

**背景**：需要调用多个外部 API（GitHub、飞书）。

**选项**：requests | httpx | aiohttp

**决策**：httpx

**理由**：性能要求 < 60s 需要并发采集能力；API 兼容 requests；同步模式可用。

### ADR-002：使用 SQLite 存储日报历史

**状态**：已采纳

**决策**：SQLite

**理由**：每天执行一次无并发写入；5 人团队数据量小；零运维。

### ADR-003：采集层返回 CollectResult

**状态**：已采纳

**决策**：引入 `CollectResult`，区分「今日无记录」与「数据获取失败」。

## 6. 非功能约束

### 6.1 错误处理

**原则**：优雅降级，不中断整体流程。

| 场景 | 处理方式 |
|------|----------|
| API 超时 | 重试 3 次（间隔 5s），仍失败标记「数据获取失败」 |
| 飞书 token 过期 | 自动刷新后重试 1 次 |
| 单个数据源不可用 | 跳过并在日报中标注，其他数据源正常采集 |
| 所有数据源不可用 | 记录错误日志，不生成空日报 |
| 推送失败 | 重试 2 次，仍失败记录日志 |

### 6.2 安全

- 所有 API 密钥通过环境变量注入，禁止硬编码
- .env 文件加入 .gitignore
- 日报中不包含代码 diff 具体内容，只含统计数据
- 只采集配置中明确列出的仓库

### 6.3 可运维性

- 日志格式：JSON lines，级别 INFO + ERROR
- 健康检查：`main.py --check`
- 配置热更新：修改 config.yaml 后下次执行自动生效

---

*规范版本：v1.1 | 已移除飞书群消息 / 群机器人*
