"""v4 日报生成器测试。

覆盖：
  - classify：分类器规则
  - formatter：Markdown/HTML 渲染（v4 结构）
  - storage：SQLite 读写
  - aggregate_members：成员聚合

注意：**不依赖 LLM 调用**（不用 mock 任何 LLM），
所有测试都跑本地函数，速度快、无副作用。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from generator.classify import CATEGORY_RULES, classify_commit_message, label_of
from generator.formatter import generate
from shared.models import (
    CommitRecord,
    FileChange,
    MemberReport,
    MessageRecord,
    TaskRecord,
)
from shared.storage import ReportStorage


# ============ 测试数据 fixture ============

def _make_commit(
    sha: str = "abc1234def5678",
    message: str = "feat: 测试提交",
    ts: datetime | None = None,
    additions: int = 100,
    deletions: int = 20,
    files_changed: int = 2,
    author: str = "lyk669",
    repo: str = "owner/repo",
    file_details: list[FileChange] | None = None,
    patch_text: str = "",
) -> CommitRecord:
    """构造一个测试用 CommitRecord。"""
    if ts is None:
        ts = datetime(2026, 10, 15, 19, 50)
    if file_details is None:
        file_details = [
            FileChange("generator/formatter.py", "modified", 60, 10),
            FileChange("generator/summarizer.py", "added", 40, 10),
        ]
    return CommitRecord(
        author=author,
        message=message,
        timestamp=ts,
        repo=repo,
        additions=additions,
        deletions=deletions,
        files_changed=files_changed,
        sha=sha,
        files=[fd.filename for fd in file_details],
        file_details=file_details,
        patch_text=patch_text,
        url=f"https://github.com/{repo}/commit/{sha}",
    )


def _make_member(
    name: str = "刘雨可",
    github: str = "lyk669",
    commits: list[CommitRecord] | None = None,
) -> MemberReport:
    return MemberReport(
        name=name,
        github_username=github,
        commits=commits or [],
    )


@pytest.fixture
def sample_commit():
    return _make_commit()


@pytest.fixture
def sample_member(sample_commit):
    return _make_member(commits=[sample_commit])


@pytest.fixture
def tmp_storage(tmp_path):
    db = tmp_path / "test.db"
    return ReportStorage(db)


# ============ 1. classify 测试 ============

class TestClassify:
    """统一分类器测试。"""

    def test_feat_prefix(self):
        assert classify_commit_message("feat: 新增日报") == "feat"

    def test_feat_with_scope(self):
        assert classify_commit_message("feat(api): 新增接口") == "feat"

    def test_fix_prefix(self):
        assert classify_commit_message("fix: 修复时区 bug") == "fix"

    def test_refactor_prefix(self):
        assert classify_commit_message("refactor: 重构渲染逻辑") == "refactor"

    def test_docs_prefix(self):
        assert classify_commit_message("docs: 补充 README") == "docs"

    def test_test_prefix(self):
        assert classify_commit_message("test: 补充单元测试") == "test"

    def test_update_keyword(self):
        """中文「更新」应归到 iterate。"""
        assert classify_commit_message("更新日报生成逻辑") == "iterate"

    def test_iterate_keyword(self):
        assert classify_commit_message("迭代日报模板") == "iterate"

    def test_delete_keyword(self):
        """「Delete」应归到 chore。"""
        assert classify_commit_message("Delete git") == "chore"

    def test_cleanup_keyword(self):
        assert classify_commit_message("清理无用文件") == "chore"

    def test_unknown_returns_none(self):
        """完全无意义的内容返回 None。"""
        assert classify_commit_message("asdf") is None
        assert classify_commit_message("xyz123") is None

    def test_empty_returns_none(self):
        assert classify_commit_message("") is None
        assert classify_commit_message(None) is None

    def test_priority_first_match_wins(self):
        """「feat: 修复 bug」应命中 feat（先匹配）。"""
        assert classify_commit_message("feat: 修复 bug") == "feat"

    def test_case_insensitive(self):
        assert classify_commit_message("FEAT: xxx") == "feat"
        assert classify_commit_message("Fix: xxx") == "fix"

    def test_label_of_known(self):
        assert label_of("feat") == "新功能"
        assert label_of("iterate") == "功能迭代"
        assert label_of("chore") == "工程维护"

    def test_label_of_none(self):
        assert label_of(None) == "其他"

    def test_label_of_unknown(self):
        assert label_of("not_a_real_key") == "其他"

    def test_rules_no_trailing_space(self):
        """规则表里所有关键词不能有尾随空格（否则匹配失效）。"""
        for key, label, keywords in CATEGORY_RULES:
            for kw in keywords:
                assert kw == kw.strip(), \
                    f"规则 '{key}' 的关键词 {kw!r} 有尾随空格"

    def test_rules_keywords_lowercase(self):
        """规则表里所有关键词应为小写（比较时 msg 会 tolower）。"""
        for key, label, keywords in CATEGORY_RULES:
            for kw in keywords:
                assert kw == kw.lower(), \
                    f"规则 '{key}' 的关键词 {kw!r} 不是小写"


# ============ 2. formatter Markdown 渲染测试 ============

class TestFormatterMarkdown:
    """v4 formatter 的 Markdown 渲染。"""

    def test_title_and_date(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "# Dev Team" in report.markdown
        assert "2026-10-15" in report.markdown
        assert "周四" in report.markdown

    def test_issue_number(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            issue_number=8,
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "第 8 期" in report.markdown

    def test_今日概览_存在(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 今日概览" in report.markdown
        assert "代码提交" in report.markdown
        assert "涉及文件" in report.markdown
        assert "代码变更" in report.markdown
        assert "飞书任务" in report.markdown
        assert "群消息" in report.markdown

    def test_工作明细_分类概览(self, sample_member):
        """工作明细应包含分类概览表格。"""
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 工作明细" in report.markdown
        assert "### 分类概览" in report.markdown
        assert "| 类型 | 占比 | 做了什么 |" in report.markdown

    def test_工作明细_分类正确(self):
        """feat 提交应归到「新功能」。"""
        c = _make_commit(message="feat: 新增功能")
        m = _make_member(commits=[c])
        report = generate(
            members=[m],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "新功能" in report.markdown

    def test_工作明细_分类迭代(self):
        """「更新...」提交应归到「功能迭代」。"""
        c = _make_commit(message="更新日报生成逻辑")
        m = _make_member(commits=[c])
        report = generate(
            members=[m],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "功能迭代" in report.markdown
        assert "其他" not in report.markdown.split("### 分类概览")[1].split("###")[0]

    def test_工作明细_提交明细(self, sample_member):
        """提交明细应包含 commit 链接、文件表。"""
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "### 提交明细" in report.markdown
        assert "查看提交" in report.markdown
        assert "| 文件 | 状态 | 增 / 删 |" in report.markdown

    def test_全天汇总(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "**全天汇总**" in report.markdown
        assert "次提交" in report.markdown
        assert "个文件" in report.markdown

    def test_无数据兜底(self):
        """无 commit 时应显示「本日无代码提交记录」。"""
        m = _make_member(commits=[])
        report = generate(
            members=[m],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "本日无代码提交记录" in report.markdown

    def test_一句话结论(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            summary="今日完成日报系统重构。",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 一句话结论" in report.markdown
        assert "今日完成日报系统重构。" in report.markdown

    def test_问题与思考_ai源(self, sample_member):
        """source=ai 时不应有 <details> 折叠。"""
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            problems=[{"text": "AI 草稿问题 1", "tag": "推测"}],
            ai_problems=[{"text": "AI 草稿问题 1", "tag": "推测"}],
            reflection_source="ai",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 问题与思考" in report.markdown
        assert "来源：AI 草稿" in report.markdown
        # ai 源不应有折叠
        qi_section = report.markdown.split("## 问题与思考")[1]
        assert "<details>" not in qi_section.split("##")[0]

    def test_问题与思考_manual源有折叠(self, sample_member):
        """source=manual 时应把 AI 草稿折叠起来。"""
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            problems=[{"text": "用户写的问题", "tag": "用户输入"}],
            ai_problems=[{"text": "AI 草稿问题", "tag": "推测"}],
            reflection_source="manual",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 问题与思考" in report.markdown
        assert "来源：用户输入" in report.markdown
        assert "<details>" in report.markdown
        assert "AI 草稿" in report.markdown

    def test_明日计划(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            next_plan=[{"text": "补充单元测试。", "tag": "推测"}],
            ai_next_plan=[{"text": "补充单元测试。", "tag": "推测"}],
            reflection_source="ai",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 明日计划" in report.markdown
        assert "补充单元测试。" in report.markdown

    def test_风险与待办(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            risks=[{"category": "数据质量", "text": "commit 不规范。"}],
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 风险与待办" in report.markdown
        assert "### 数据质量" in report.markdown
        assert "commit 不规范。" in report.markdown

    def test_飞书协作动态_有消息(self, sample_member):
        msg = MessageRecord(
            sender_id="ou_xxx",
            sender_name="刘雨可",
            chat_id="oc_xxx",
            text="今天代码已推",
            msg_type="text",
            timestamp=datetime(2026, 10, 15, 19, 56),
        )
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            feishu_messages=[msg],
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "## 飞书协作动态" in report.markdown
        assert "原始消息" in report.markdown
        assert "今天代码已推" in report.markdown

    def test_飞书任务_状态流转(self, sample_member):
        task = TaskRecord(
            assignee="刘雨可",
            title="接入群消息采集",
            status_from="进行中",
            status_to="已完成",
            updated_at=datetime(2026, 10, 15, 19, 20),
        )
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            tasks=type("TasksObj", (), {"records": [task]})(),
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "任务进展" in report.markdown
        assert "进行中 → 已完成" in report.markdown


# ============ 3. formatter HTML 渲染测试 ============

class TestFormatterHtml:
    """v4 formatter 的 HTML 渲染。"""

    def test_html_wrapper(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "<html>" in report.html
        assert "</html>" in report.html

    def test_html_has_table(self, sample_member):
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "<table" in report.html

    def test_html_escapes_user_input(self, sample_member):
        """用户输入含 <script> 应被转义。"""
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            problems=[{"text": "<script>alert(1)</script>", "tag": "用户输入"}],
            ai_problems=[],
            reflection_source="manual",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "<script>" not in report.html
        assert "&lt;script&gt;" in report.html

    def test_html_无数据(self):
        m = _make_member(commits=[])
        report = generate(
            members=[m],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "本日无代码提交记录" in report.html


# ============ 4. storage 测试 ============

class TestStorage:
    """SQLite 存储测试。"""

    def test_save_and_get_latest(self, tmp_storage):
        rid = tmp_storage.save(
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            markdown="# test",
            html="<html></html>",
            generated_at=datetime(2026, 10, 15, 20, 0),
            payload={"summary": "hello"},
        )
        assert rid > 0

        latest = tmp_storage.get_latest_report()
        assert latest is not None
        assert latest["markdown"] == "# test"
        assert latest["payload"]["summary"] == "hello"

    def test_save_overwrites_same_date(self, tmp_storage):
        tmp_storage.save(
            date(2026, 10, 15), "Dev Team", "v1", "<html></html>",
            datetime(2026, 10, 15, 20, 0),
        )
        tmp_storage.save(
            date(2026, 10, 15), "Dev Team", "v2", "<html></html>",
            datetime(2026, 10, 15, 21, 0),
        )
        latest = tmp_storage.get_latest_report()
        assert latest["markdown"] == "v2"
        assert tmp_storage.count_reports() == 1

    def test_count_reports(self, tmp_storage):
        assert tmp_storage.count_reports() == 0
        tmp_storage.save(
            date(2026, 10, 14), "Dev Team", "d1", "<html></html>",
            datetime(2026, 10, 14, 20, 0),
        )
        tmp_storage.save(
            date(2026, 10, 15), "Dev Team", "d2", "<html></html>",
            datetime(2026, 10, 15, 20, 0),
        )
        assert tmp_storage.count_reports() == 2

    def test_get_yesterday_stats(self, tmp_storage):
        yesterday = date(2026, 10, 14)
        payload = {
            "members": [
                {"commits": [
                    {"message": "feat: A", "additions": 100, "deletions": 20,
                     "files_changed": 2},
                    {"message": "fix: B", "additions": 30, "deletions": 10,
                     "files_changed": 1},
                ]},
            ],
            "feishu_messages": [{"text": "x"}, {"text": "y"}],
        }
        tmp_storage.save(
            yesterday, "Dev Team", "d1", "<html></html>",
            datetime(2026, 10, 14, 20, 0),
            payload=payload,
        )

        stats = tmp_storage.get_yesterday_stats(date(2026, 10, 15))
        assert stats is not None
        assert stats["commits"] == 2
        assert stats["files"] == 3
        assert stats["additions"] == 130
        assert stats["deletions"] == 30
        assert stats["msgs"] == 2

    def test_get_yesterday_stats_no_data(self, tmp_storage):
        """昨天没数据时返回 None。"""
        assert tmp_storage.get_yesterday_stats(date(2026, 10, 15)) is None

    def test_save_and_get_reflection(self, tmp_storage):
        tmp_storage.save_reflection(
            report_date=date(2026, 10, 15),
            problems=[{"text": "用户问题", "tag": "用户输入"}],
            next_plan=[{"text": "用户计划", "tag": "用户输入"}],
            ai_problems=[{"text": "AI 问题", "tag": "推测"}],
            ai_next_plan=[{"text": "AI 计划", "tag": "推测"}],
            source="manual",
        )
        r = tmp_storage.get_reflection_by_date(date(2026, 10, 15))
        assert r is not None
        assert r["source"] == "manual"
        assert len(r["problems"]) == 1
        assert r["problems"][0]["text"] == "用户问题"

    def test_get_recent_reflections_only_user(self, tmp_storage):
        # 一条 AI 源，一条 manual 源
        tmp_storage.save_reflection(
            date(2026, 10, 14),
            problems=[], next_plan=[], source="ai",
        )
        tmp_storage.save_reflection(
            date(2026, 10, 15),
            problems=[{"text": "用户写的", "tag": "用户输入"}],
            next_plan=[],
            source="manual",
        )
        results = tmp_storage.get_recent_reflections(
            days=7, end_date=date(2026, 10, 15), only_user_written=True,
        )
        assert len(results) == 1
        assert results[0]["source"] == "manual"


# ============ 5. 端到端测试（mini pipeline） ============

class TestEndToEnd:
    """不调 LLM，验证 formatter + storage 协同。"""

    def test_full_report_generation(self, sample_member, tmp_storage):
        """生成 → 存 → 读回，数据一致。"""
        report = generate(
            members=[sample_member],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            summary="今日完成日报重构。",
            highlights=["重构日报生成", "新增分类器"],
            commit_summaries={
                "abc1234def5678": "重构日报生成器，新增统一分类器。",
            },
            issue_number=8,
            generated_at=datetime(2026, 10, 15, 20, 0),
        )

        rid = tmp_storage.save(
            report.date, report.team_name,
            report.markdown, report.html, report.generated_at,
        )
        assert rid > 0

        latest = tmp_storage.get_latest_report()
        assert latest["markdown"] == report.markdown
        assert "统一分类器" in latest["markdown"]

    def test_多成员渲染(self):
        """两个成员各自的 commit 应分开显示。"""
        c1 = _make_commit(sha="a1111", message="feat: A", author="lyk669")
        c2 = _make_commit(sha="b2222", message="fix: B", author="zhangsan")
        m1 = _make_member("刘雨可", "lyk669", [c1])
        m2 = _make_member("张三", "zhangsan", [c2])
        report = generate(
            members=[m1, m2],
            report_date=date(2026, 10, 15),
            team_name="Dev Team",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "feat: A" in report.markdown
        assert "fix: B" in report.markdown


# ============ 6. 边界情况 ============

class TestEdgeCases:
    def test_commit_no_files(self):
        """commit 没有 file_details 不应崩。"""
        c = CommitRecord(
            author="x", message="feat: 空", timestamp=datetime(2026, 10, 15, 10, 0),
            repo="o/r", additions=0, deletions=0, files_changed=0,
            sha="abc", file_details=[],
        )
        m = _make_member(commits=[c])
        report = generate(
            members=[m], report_date=date(2026, 10, 15),
            team_name="X", generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert "feat: 空" in report.markdown

    def test_commit_message_with_pipe(self):
        """commit message 含 | 不应破坏表格。"""
        c = _make_commit(message="feat: a|b")
        m = _make_member(commits=[c])
        report = generate(
            members=[m], report_date=date(2026, 10, 15),
            team_name="X", generated_at=datetime(2026, 10, 15, 20, 0),
        )
        # 表头应仍为 3 列
        assert "| 文件 | 状态 | 增 / 删 |" in report.markdown

    def test_empty_members_list(self):
        """空成员列表不应崩。"""
        report = generate(
            members=[],
            report_date=date(2026, 10, 15),
            team_name="X",
            generated_at=datetime(2026, 10, 15, 20, 0),
        )
        assert report.markdown