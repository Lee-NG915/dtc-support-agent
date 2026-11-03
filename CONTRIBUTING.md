# 贡献指南

## 依赖管理

依赖的唯一来源是 `pyproject.toml`：运行时依赖写在 `[project.dependencies]`，可选组写在 `[project.optional-dependencies]`（`dev`、`rerank`、`docs`）。仓库不再维护单独的 `requirements.txt`，避免两份版本清单漂移。

`uv.lock` 是提交进仓库的锁定版本清单，用于复现完整依赖树。CI 会跑 `uv lock --check`，一旦它与 `pyproject.toml` 不同步就会失败。

```bash
uv sync --extra dev          # 按 uv.lock 精确复现开发环境（推荐）
uv lock --check              # 改过 pyproject 后校验锁文件是否同步

pip install -e ".[dev]"      # 开发（含 pytest、ruff、mypy）
pip install -e ".[rerank]"   # 本地 bge 召回 + Cross-Encoder 精排
pip install -e ".[docs]"     # Unstructured 解析 PDF/Office/HTML/图片
```

## 代码规范

项目使用 Python 3.11+，统一采用 Ruff 进行格式化、导入排序和静态检查，并用 mypy 做类型检查。新增模块应包含模块级文档字符串；公开的类、函数和异步接口应使用 Google 风格 docstring，说明用途、参数、返回值及异常。

```bash
ruff format .
ruff check .
mypy
python -m compileall -q app eval
pytest -q
```

CI 会在 Python 3.11 与 3.12 上依次跑 Ruff、mypy 与 pytest，并要求 `app` / `eval` 的覆盖率不低于 70%。

提交前请确保不提交 `.env`、密钥、数据库文件和评测生成物。业务逻辑应放在分层目录中，通过 schema 传递数据，避免在路由层直接访问基础设施。

## 项目技能与文档

开发助手先读 [AGENTS.md](AGENTS.md) 和 [项目档案](.dtc/target-profile.md)。技能安装在 `.agents/skills/`；不要放入应用的 `app/skills/`。调用与更新见 [技能指南](docs/guides/skills.md)。

文档遵循 [文档规范](docs/guides/documentation.md)。业务能力变化同步当前主题、真实源码入口、学习解释和受影响验证；纯文档/技能改动运行：

```bash
python3 scripts/check_project_docs.py
git diff --check
```

检查器只验证本项目维护文档和开发技能的结构、本地链接与元数据；不验证外部链接、语义或应用运行，也不遍历改写全部上游教程。代码变更仍按上面的工程检查和实际影响验证。新检查器目前是本地步骤，未接入现有 CI。
