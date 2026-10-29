# UB-Tools — UB-Mode Quality Gate Toolchain

> **确定性代码质量门禁工具**，为 `ub-mode` 三段式工程纪律提供自动化检测能力。

## 工具清单

| 工具 | 用途 | 阈值 | 典型调用 |
|------|------|------|---------|
| **crapper** | CRAP 分数（复杂度 + 覆盖率） | CRAP < 10 | `ub-quality crapper --root . --threshold 10` |
| **dryer** | 结构性代码重复检测 | 相似度 < 0.8 | `ub-quality dryer --root . --threshold 0.8` |
| **mutator** | 变异测试 | 存活率 = 0 | `ub-quality mutator --root . --test-command "pytest"` |

## 快速开始

### 安装

```bash
cd ub-mode
python3 -m venv .venv
.venv/bin/pip install -e tools/
```

### 使用

```bash
# 统一入口（推荐）
tools/bin/ub-quality crapper --root /path/to/project
tools/bin/ub-quality dryer --root /path/to/project
tools/bin/ub-quality mutator --root /path/to/project --test-command "pytest"

# 或者直接使用 venv 中的入口
.venv/bin/crapper --root ./src
.venv/bin/dryer --root ./src
.venv/bin/mutator --root ./src --test-command "npm test"

# 一键全检
tools/bin/ub-quality all /path/to/project
```

## CRAP 评分公式

```
CRAP = CC² × (1 − coverage)³ + CC

CC       = 圈复杂度 (Cyclomatic Complexity)
coverage = 测试覆盖率 (0..1)

CRAP < 10 → ✓ 通过
CRAP ≥ 10 → ✗ 需要重构
```

## 支持的语言

| 语言 | crapper | dryer | mutator | 解析引擎 |
|------|:-------:|:-----:|:-------:|---------|
| Python | ✓ | ✓ | ✓ | built-in `ast` |
| TypeScript | ✓ | ✓ | ✓ | tree-sitter |
| JavaScript | ✓ | ✓ | ✓ | tree-sitter |
| Vue SFC | — | — | — | 后续计划 |

## DSH 环境配置

```bash
# 确认工具可用
/Users/yi/YrY/ub-mode/tools/bin/ub-quality env

# 将工具加入 PATH（可选）
export PATH="$PATH:/Users/yi/YrY/ub-mode/tools/bin"
```

## 配套资源

| 资源 | 位置 |
|------|------|
| ub-mode SKILL.md | `../SKILL.md` |
| Bot 配置 | `../../YiKnowledge/roles/ub-mode/BOT.md` |
| 日报 | `../../YiKnowledge/roles/ub-mode/reports/` |
| 项目 README | `../../YiKnowledge/projects/ub-mode/README.md` |

## 架构

```
ub-mode/
├── SKILL.md              # ub-mode 技能定义
├── .venv/                # Python 虚拟环境 (tree-sitter + ub-tools)
├── tools/                # 质量门禁工具包
│   ├── pyproject.toml    # 包配置
│   ├── bin/
│   │   └── ub-quality    # 统一启动器 (bash)
│   └── ub_tools/
│       ├── __init__.py
│       ├── shared.py     # 共享工具（tree-sitter 解析、CRAP 公式）
│       ├── crapper.py    # CRAP 分数计算
│       ├── dryer.py      # 重复代码检测
│       └── mutator.py    # 变异测试
└── YiKnowledge/roles/ub-mode/
    └── reports/          # 日报归档
```

## 设计原则

1. **确定性** — 相同输入产生相同输出，不依赖主观判断
2. **零网络依赖** — 所有工具本地运行，无需外网访问
3. **DSH 原生** — 工具可通过 `ub-quality` 或 Python API 调用
4. **渐进增强** — 从最少功能开始（Python 解析），逐步扩展语言支持
5. **与 Uncle Bob 工具兼容** — 遵循相同的 CLI 接口和输出格式