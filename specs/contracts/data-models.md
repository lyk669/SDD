# 智能日报生成器 — 数据模型定义

## 1. 采集层输出模型

### CollectResult（采集结果封装，ADR-003）

| 字段 | 类型 | 说明 |
|------|------|------|
| success | bool | 采集是否成功 |
| records | list[T] | 记录列表；成功且为空表示「今日无记录」 |
| error_message | str \| None | 失败时的错误信息 |

采集层 `collect()` 均返回 `CollectResult[...]`，不再返回裸列表。

### CommitRecord（代码提交记录）

| 字段 | 类型 | 说明 |
|------|------|------|
| author | str | 提交者（GitHub 用户名） |
| message | str | commit message |
| timestamp | datetime | 提交时间 |
| repo | str | 仓库名称 |
| additions | int | 新增行数 |
| deletions | int | 删除行数 |
| files_changed | int | 变更文件数 |

### TaskRecord（任务变更记录）

| 字段 | 类型 | 说明 |
|------|------|------|
| assignee | str | 负责人（飞书用户名 / open_id 等） |
| title | str | 任务标题 |
| status_from | str | 原状态 |
| status_to | str | 新状态 |
| updated_at | datetime | 变更时间 |

## 2. 生成层模型

### MemberReport（成员日报）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | str | 成员姓名 |
| github_username | str | GitHub 用户名 |
| commits | list[CommitRecord] | 代码提交列表 |
| tasks | list[TaskRecord] | 任务变更列表 |
| commits_failed | bool | 代码提交数据源是否失败（ADR-003） |
| tasks_failed | bool | 任务数据源是否失败 |

身份映射：`config.yaml` 的 `members` 除 `lark`（邮箱等）外，可选手填 `lark_open_id` / `lark_user_id`，聚合时与采集到的 assignee 多键对齐。

### DailyReport（日报）

| 字段 | 类型 | 说明 |
|------|------|------|
| date | date | 日报日期 |
| team_name | str | 团队名称 |
| members | list[MemberReport] | 各成员数据 |
| generated_at | datetime | 生成时间 |
| markdown | str | Markdown 格式日报 |
| html | str | HTML 格式日报 |

## 3. 身份映射

```yaml
members:
  - name: "张三"
    github: "zhangsan"
    lark: "zhangsan@company.com"
  - name: "李四"
    github: "lisi-dev"
    lark: "lisi@company.com"
```

## 4. 模块接口契约

### 采集层

```python
# collector/github.py
def collect(repos: list[str], since: datetime, until: datetime) -> CollectResult[CommitRecord]

# collector/lark_task.py
def collect(tasklist_id: str, since: datetime, until: datetime) -> CollectResult[TaskRecord]
```

### 生成层

```python
# generator/formatter.py
def generate(members: list[MemberReport], date: date, team_name: str) -> DailyReport
```

### 推送层

```python
# notifier/email.py
def send(report: DailyReport, recipients: list[str]) -> bool
```

---

*规范版本：v1.1 | 已移除飞书群消息 / 群机器人相关契约*
