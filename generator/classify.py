"""统一的 commit 分类器。

formatter.py（渲染分类概览）和 summarizer.py（风险检测）
都从这里导入，避免两套规则打架。

规则表从上到下匹配，先命中先用。
匹配词统一小写、无尾随空格。
"""

from __future__ import annotations


# ============ 规则表（唯一真相） ============

CATEGORY_RULES = [
    # (key, 中文label, 匹配词)
    ("feat",     "新功能",   ["feat:", "feature:", "add:", "new feature", "新增", "添加"]),
    ("fix",      "修复",     ["fix:", "bugfix:", "hotfix:", "bug:", "patch:", "修复", "修正"]),
    ("refactor", "重构",     ["refactor:", "重构", "rewrite", "重写"]),
    ("perf",     "优化",     ["perf:", "优化", "optimize", "improve"]),
    ("test",     "测试",     ["test:", "tests:", "测试", "unittest", "pytest"]),
    ("docs",     "文档",     ["docs:", "doc:", "文档", "readme", "更新说明", "补充说明"]),
    ("iterate",  "功能迭代", ["更新", "迭代", "update", "升级"]),
    ("chore",    "工程维护", [
        "chore:", "ci:", "build:", "style:", "配置", "构建",
        "删除", "清理", "delete", "remove", "clean",
        "gitignore", "轮转", "rotate", "logs/",
    ]),
]


# ============ 对外接口 ============

def classify_commit_message(msg: str) -> str | None:
    """返回类别 key（如 "feat" / "iterate" / "chore"）。

    完全不认识时返回 None。
    """
    if not msg:
        return None
    m = msg.strip().lower()
    for key, _label, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in m:
                return key
    return None


def label_of(key: str | None) -> str:
    """key → 中文 label。None 或未知 key 返回 "其他"。"""
    for k, label, _ in CATEGORY_RULES:
        if k == key:
            return label
    return "其他"