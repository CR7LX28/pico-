"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的"仓库第一印象"。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import subprocess
import textwrap
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
# 白名单文档——只给模型一小份"导航包"，不预加载整个仓库。
# AGENTS.md     — 规定工作方式、测试方式、提交约束
# README.md     — 交代项目结构、启动命令和目录入口
# pyproject.toml — 暴露 Python 项目结构和工具链
# package.json  — 快速说明是否是 Node 项目、脚本入口在哪
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
IGNORED_PATH_NAMES = {".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


class WorkspaceContext:
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        """采集"仓库第一印象"，生成 WorkspaceContext 对象。

        整个采集流程极简且容错：所有 git 操作都可能失败，但都有 fallback，
        绝不会因为"不在 git 仓库"或"没有网络"就启动不了。采集的信息量也刻意
        很少（最近 5 条 commit、文档 clip 到 1200 字符），目的是给模型一个
        快速背景印象，而不是把整个仓库塞进 prompt。

        参数:
            cwd: 启动目录。CLI 通过 args.cwd 传入，默认 "."。
            repo_root_override: 强制指定仓库根目录。绝大多数为 None（git 自动探测）。
                唯一传非 None 的场景是评测框架（evaluator.run_task），
                在临时拷贝的 fixture 上运行（非 git 仓库），需要显式指定仓库根。
        """
        cwd = Path(cwd).resolve()  # 转为绝对路径，消除 ~ / .. / 符号链接等歧义

        def git(args, fallback=""):  # 轻量级 git 包装器（闭包，自动捕获外层 cwd）
            try:
                result = subprocess.run(
                    ["git", *args],          # 解包拼成完整命令
                    cwd=cwd,                 # 在工作目录下执行
                    capture_output=True,     # 捕获 stdout/stderr，不打印到终端
                    text=True,               # 输出按字符串返回（而非 bytes）
                    check=True,              # 非零退出码自动抛 CalledProcessError
                    timeout=5,               # 超时 5 秒，防止卡死
                )
                return result.stdout.strip() or fallback  # 空字符串时短路降级为 fallback
            except Exception:  # 捕获所有异常（CalledProcessError / TimeoutExpired 等），统一降级
                return fallback

        repo_root = (  # 确定仓库根目录
            Path(repo_root_override).resolve()             # repo_root_override 非 None 时强制用它
            if repo_root_override is not None
            else Path(git(                                  # 否则 git rev-parse 自动探测；
                ["rev-parse", "--show-toplevel"], str(cwd)  # 不在 git 仓库时 fallback 为 cwd（当前目录当仓库根）
            )).resolve()
        )
        docs = {}
        for base in (repo_root, cwd):       # 同时扫两个目录，子目录启动时也能看到本地文档
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                key = str(path.relative_to(repo_root))  # 用相对路径做 key，避免重复收集
                if key in docs:
                    continue
                docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)  # clip 到 1200 字符

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",  # 当前分支名；不在任何分支时 fallback 为 "-"
            default_branch=(  # 远程默认分支，去掉 "origin/" 前缀（origin/main → main）
                lambda branch: branch[len("origin/") :] if branch.startswith("origin/") else branch
            )(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),  # git status --short，clip 到 1500
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],  # 最近 5 条 commit
            project_docs=docs,
        )

    def text(self):
        # 这段文本会被塞进 prompt prefix，作为相对稳定的基线上下文。
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {self.cwd}
            - repo_root: {self.repo_root}
            - branch: {self.branch}
            - default_branch: {self.default_branch}
            - status:
            {self.status}
            - recent_commits:
            {commits}
            - project_docs:
            {docs}
            """
        ).strip()

    def fingerprint(self):  # 判断仓库状态是否变化，决定是否需要重建缓存中的 prompt prefix
        payload = {                                        # 将所有字段打包成 dict，作为指纹来源
            "cwd": self.cwd,                               # 当前工作目录
            "repo_root": self.repo_root,                   # 仓库根目录
            "branch": self.branch,                         # 当前分支
            "default_branch": self.default_branch,         # 默认分支
            "status": self.status,                         # git status 输出
            "recent_commits": list(self.recent_commits),   # 最近提交（转 list 确保可序列化）
            "project_docs": dict(self.project_docs),       # 项目文档（转 dict 确保可序列化）
        }
        return hashlib.sha256(                             # SHA-256 哈希
            json.dumps(payload, sort_keys=True)            # JSON 序列化，sort_keys 保证字段顺序不影响结果
                .encode("utf-8")                           # 转为 bytes（hashlib 只接受 bytes）
        ).hexdigest()                                      # 返回十六进制字符串（64 位）
