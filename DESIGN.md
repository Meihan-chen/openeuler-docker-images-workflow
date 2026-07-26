# openEuler 容器镜像自动化系统设计

> 版本 2.1，基于 openEuler 社区规范、现有 Agent 实验及 easysoftware-autoupgrade 实践的综合研究

---

## 目录

1. [系统概览](#1-系统概览)
2. [仓库规范与目录结构](#2-仓库规范与目录结构)
3. [核心工作流设计](#3-核心工作流设计)
4. [Agent 协作架构](#4-agent-协作架构)
5. [测试策略](#5-测试策略)
6. [构建流水线](#6-构建流水线)
7. [版本管理](#7-版本管理)
8. [PR 自动化规范](#8-pr-自动化规范)
9. [技术选型](#9-技术选型)
10. [风险与缓解](#10-风险与缓解)
11. [项目结构](#11-项目结构)

---

## 1. 系统概览

### 1.1 定位

通过 GitHub Actions 部署，实现 openEuler 应用容器镜像的全自动化创建、更新和维护。由确定性代码承载主流程，Agent 处理不确定性决策，构成对抗协作闭环。

### 1.2 核心场景

| 场景 | 触发方式 | 输出 |
|------|---------|------|
| 新增应用镜像 | GitCode Issue（`【new-image】` 模板） | PR：完整镜像目录 + 文档 + 测试 |
| 应用版本更新 | anitya 监控触发 / workflow_dispatch | PR：新增版本子目录 |
| openEuler 大版本升级 | 监控新 oe 版本 / workflow_dispatch | 批量 PR：所有应用新增 oe 版本子目录 |

### 1.3 设计原则

- **确定性承载不确定**：主流程由代码承载（GitHub Actions Workflow + Python），不可预见的决策（Dockerfile 编写、故障诊断、测试生成）由 Agent 处理
- **只能新增，不能修改**：已有镜像目录为只读，新版本只能追加新目录
- **Agent 对抗协作**：Image Creator + Image QA、Testcase Creator + Testcase QA 构成两个 QA 对抗对，生成者在 QA 挑战下修正输出，通过后再进入本地验证
- **本地验证**：提 PR 前在 Actions Runner 上完成双架构构建 + 测试，结果内嵌 PR 描述
- **一次性执行模型**：每次事件（webhook / workflow_dispatch）触发后独立运行，执行完毕后退出（参考 easysoftware 的 `System.exit(0)` 模式）

### 1.4 与 easysoftware-autoupgrade 的关系

`easysoftware-autoupgrade` 是 openEuler 社区已有的容器镜像自动升级系统（Java SpringBoot 应用），与本系统操作同一目标仓库 `openeuler/openeuler-docker-images`。我们从它的实现中提取了已验证的模式并作出适应性调整：

**采纳的模式：**

| 模式 | easysoftware 的实现 | 本系统的采用 |
|------|--------------------|-------------|
| 一次性执行 | `Application.main()` 调用任务后 `System.exit(0)`，由 cron 容器定时拉起 | 每个 GitHub Actions Workflow 独立运行，编排器执行完毕后退出。由 anitya webhook 或 `workflow_dispatch` 触发 |
| 上游版本监控 | easysoftware 的 `projectsInfoUrl` 数据来自 anitya，覆盖 GitHub / PyPI / npm / Rubygems / CPAN 等生态的上游版本跟踪 | **直接复用**。应用名作为 anitya 查找 key（`projectsInfoUrl + appName`），新应用在 `doc/image-info.yml` 中增加 `upstream` 字段声明 `backend` 和 `homepage`（见 2.3 节），新版本由 webhook 触发，无需轮询 |
| PR 去重 | `checkHasCreatePR()` 遍历已有 open PR，按 title 精确匹配，命中则跳过 | 同样按 title 匹配去重，PR 生成前检查，防止重复提交 |
| 批量更新 | `batchUpdatePremiumApp()` 遍历 DockerHub 上所有 openEuler OS 版本，对缺最新镜像的版本生成 Dockerfile | oe-upgrade 场景的 Matrix 并行策略直接借鉴此模式 |
| 差异化 Dockerfile | 复制已有版本的目录树，替换 Dockerfile 中的版本字符串，更新 meta.yml | Creator Agent 在版本更新场景下的标准操作：复制 → 替换 → 追加 |

**调整的决策：**

| 维度 | easysoftware | 本系统选择 | 原因 |
|------|-------------|-----------|------|
| 文件操作 | easysoftware 通过 Git Data API 直接操作 tree/blob，不 clone | **Git clone → modify → commit → push** | 我们需要 build + test 验证，必须有本地文件系统；API 模式适合纯文本替换，不适合构建验证 |
| 分支策略 | Fork 到 bot 账号 → 跨仓库 PR | **Bot token 直接推分支 → PR** | GitCode 兼容 GitHub API v3，bot token 有直接写权限时不需要 fork 链路 |
| 构建验证 | 无（由仓库 CI 单独处理） | **PR 生成前在 Runner 上完成** | 这是需求的硬要求：提 PR 前必须证明 Dockerfile 可构建且通过测试 |
| 运行载体 | SpringBoot 独立部署 | **GitHub Actions** | 这是系统部署约束：必须通过 GitHub Actions 部署 |

**互补关系：** 两个系统操作同一仓库但触发条件和覆盖范围不同。easysoftware 侧重持续性的版本跟踪和批量升级，本系统在此基础上增加了 Issue 驱动的新镜像创建、Agent 对抗协作质量保障、双架构构建验证。两者可以共享版本检测服务，互不冲突。

#### easysoftware 完整接口清单

通过分析 `EasysoftwareVersionHelper`、`CollectConfig`、`ApplicationVersionTask`、`ApplicationUpdateHandler` 等源码，梳理出 easysoftware 的全部上游接口及操作模式。本设计明确注明每个接口的采用策略。

**上游接口（数据获取）：**

| 接口 | 后端 | 用途 | 来源文件 | 本系统采用 |
|------|------|------|---------|-----------|
| `projectsInfoUrl` + appName | anitya | 返回上游最新版本（`app_up`）、社区版本（`app_openeuler`）、OS 版本（`raw_versions`） | `EasysoftwareVersionHelper.initUpdateInfo()` | **直接复用** |
| `backupProjectsInfoUrl` + appName | anitya（备用） | 主源返回空时的 fallback | 同上 | 复用 |
| `apppkgInfoUrl` + 分页（pageSize=50） | 软件市场 | 获取全量"精品应用"清单（name + pkgId） | `EasysoftwareVersionHelper.getEasysoftApppkgSet()` | **直接复用** |
| `apppkgDetailUrl` + pkgId | 软件市场 | 获取应用详情（维护者邮箱等） | `EasysoftwareVersionHelper.setAppkgMail()` | 复用 |
| `openEulerOsVersionInfoUrl` | datastat.openeuler.org | 获取 openEuler 最新 OS 版本 | `EasysoftwareVersionHelper.getOpeneulerLatestOsVersion()` | **直接复用** |
| `dockerHubOpeneulerOsVersionInfoUrl` | DockerHub | 获取 openEuler 全部 OS 版本列表 | `EasysoftwareVersionHelper.getDockerHubOpeneulerOsVersion()` | 复用 |

**下游操作：**

easysoftware 通过 API 完成 fork → branch → tree API → commit → PR 的调用链。本系统操作 GitCode 仓库，通过 GitCode API（兼容 GitHub API v3）实现对应操作，核心差异在于：

- **不 fork**：GitCode 的 bot token 可直接推分支到目标仓库，无需 fork 链路
- **clone 而非 API 操作文件**：本系统需要 build + test 验证，必须有完整本地文件系统
- **PR 去重后创建**：逻辑与 easysoftware 一致，但调用 GitCode PR API

easysoftware 的 SMTP 邮件通知改为 GitHub Actions workflow 通知 + PR/Issue 描述。

---

## 2. 仓库规范与目录结构

### 2.1 目标仓库顶层

```
openeuler-docker-images/
├── AI/                  # AI 软件栈
├── Base/                # 基础镜像（openeuler 基础镜像的 Dockerfile）
├── Bigdata/             # 大数据组件
├── Storage/             # 存储组件
├── Database/            # 数据库
├── Cloud/               # 云原生
├── Distroless/          # 无发行版
├── HPC/                 # 高性能计算
├── Security/            # 安全相关
├── Others/              # 其他
├── config/              # 仓库配置
└── tests/               # 历史测试套件（不再新增，新应用用 <app>/tests/）
```

每个场景目录含 `image-list.yml`，维护应用名到路径的映射。

### 2.2 最小目录单元（MDU）

```
<app-name>/
├── README.md                        # 快速参考、功能描述、Tag 表、使用方法、FAQ
├── meta.yml                         # Tag 与 Dockerfile 路径映射
├── doc/                             # 软件中心展示（可选）
│   ├── picture/logo.png
│   └── image-info.yml
└── <app-version>/                   # 如 1.27.2
    └── <oe-version>/                # 如 24.03-lts
        └── Dockerfile
```

### 2.3 doc/image-info.yml

目标仓库当前的 `doc/image-info.yml` 包含 `name`、`category`、`description`、`environment`、`tags`、`download`、`usage`、`license`、`similar_packages`、`dependency` 等字段，**不含版本监控配置**。

easysoftware 通过应用名直接查询 `projectsInfoUrl`（`projectsInfoUrl + appName`），anitya 内部以项目名作为查找 key。因此无需引入 `project_id`——应用名即是监控查找的天然键。

新增 `upstream` 字段用于声明上游信息。对已有应用可选（anitya 已注册），对新应用必填（Creator Agent 需要这些字段在 anitya 中注册项目）：

```yaml
# ... 已有字段 (name, category, description, 等) ...

upstream:                    # 版本监控信息
  backend: GitHub            # anitya backend: GitHub / PyPI / npm / Rubygems / CPAN / custom
  homepage: https://github.com/apache/spark
```

**`upstream` 字段说明：**

| 字段 | 必填 | 说明 |
|------|------|------|
| `backend` | 新应用必填 | 版本获取后端：`GitHub`、`PyPI`、`npm`、`Rubygems`、`CPAN`、`custom`。对应 anitya `Project.backend` |
| `homepage` | 新应用必填 | 上游项目主页 URL。Creator Agent 可从 Issue 的 `source_repo_url` 推导 |

**版本查找逻辑：** 系统以 `doc/image-info.yml` 的 `name` 字段作为 key 查询 `projectsInfoUrl`。如果 anitya 中该项目不存在（新应用），Creator Agent 用 `upstream` 中的 `backend` 和 `homepage` 在 anitya 中注册项目，注册后即可通过 name 查询版本。

### 2.4 meta.yml

```yaml
# Tag: <app-ver>-oe<oe-ver-short>
3.3.1-oe2203lts:
  path: spark/3.3.1/22.03-lts/Dockerfile
3.3.2-oe2203lts:
  path: spark/3.3.2/22.03-lts/Dockerfile
  arch: aarch64          # 可选；省略 = 双架构
```

### 2.5 Dockerfile 规范

```dockerfile
ARG BASE=openeuler/openeuler:24.03-lts-sp2
FROM ${BASE}

RUN yum install -y <packages> && \
    yum clean all
```

- 基础镜像：`openeuler/openeuler:<version>`
- 包管理器：`yum`
- 多阶段构建用于编译型应用

### 2.6 "只能新增"约束实现

1. 本地差分检查：`git diff --name-only origin/main...HEAD`，拒绝已有文件修改
2. meta.yml 和 README.md 为例外：仅允许在末尾追加条目（结构化解析验证）
3. 幂等检查：解析已有 `meta.yml` 确认 `<app-ver, oe-ver>` 组合不存在
4. PR 去重：提 PR 前遍历目标仓库已有 open PR，按 title 精确匹配。若已存在同名 PR（如 `[version-update] nginx 1.28.0 on 24.03-lts`），则跳过不创建。title 命名规则保证相同场景+相同应用+相同版本的 PR 只会存在一个。此机制直接借鉴 easysoftware 的 `checkHasCreatePR()`——其遍历所有 open PR（每页 100 条，多页），按 title 匹配判断是否已存在

---

## 3. 核心工作流设计

### 3.1 整体架构

```
                    ┌──────────────────────────┐
                    │   GitHub Actions 触发层    │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │     确定性编排器           │
                    │     (run.py)              │
                    └──────────┬───────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │  并行启动           │                    │
          ▼                    ▼                    │
   ┌─────────────┐     ┌─────────────┐              │
   │ 对抗对一     │     │ 对抗对二     │              │
   │             │     │             │              │
   │ Creator → QA│     │ Creator → QA│              │
   │ (生成↔挑战) │     │ (生成↔挑战) │              │
   └──────┬──────┘     └──────┬──────┘              │
          │  QA 认可后        │  QA 认可后          │
          └────────┬──────────┘                     │
                   │                                │
                   ▼                                │
        ┌──────────────────────┐                    │
        │   本地验证（确定性代码） │                    │
        │  构建 + 测试 + 差分    │                    │
        └──────────┬───────────┘                    │
                   │                                │
         ┌─────────┴─────────┐                      │
         ▼                   ▼                      │
      通过                 失败                      │
         │                   │                      │
         ▼                   ▼                      │
  ┌──────────┐       ┌──────────────┐               │
  │  提交 PR  │       │    Fixer     │               │
  │ (附两对    │       │  (分析+修复)  │               │
  │  对抗记录) │       └──────┬───────┘               │
  └──────────┘              │                       │
                            │ ← ← ← loop             │
                            │ (最多 3 轮)             │
                            ▼                       │
                     ┌──────────────┐               │
                     │ needs-human  │               │
                     │   -review    │               │
                     └──────────────┘               │
```

**关键时序：**
- 两个对抗对**并行启动**，对内部 Creator 先生成 → QA 挑战 → Creator 修正（最多 2 轮）→ QA 认可
- 两对都完成后进入本地验证
- Fixer **仅在本地验证失败后介入**，与本地验证形成 loop（最多 3 轮）
- 本地验证通过是提 PR 的唯一路径

### 3.2 场景一：新增应用镜像

```
GitCode Issue 创建 [new-image 标签]
  → Webhook / Polling Bridge
    → parse_issue.py 解析 (package_name, source_repo_url, domain, os_version)
      → 两个对抗对并行启动：
        ┌ 对抗对一：Image Creator 生成 → Image QA 挑战 → Creator 修正（最多 2 轮）→ QA 认可
        └ 对抗对二：Testcase Creator 生成 → Testcase QA 挑战 → Creator 补充（最多 2 轮）→ QA 认可
      → docker build（x86_64 + ARM64 并行）+ 测试执行
        ┌─ 通过？→ gate_diff.py → compose_pr.py → 提 PR（附两个对抗对的审查记录）
        └─ 失败？→ Fixer 分析日志 + 修复 → 重新本地验证
                                            ↑                    │
                                            └── loop（最多 3 轮）──┘
            3 轮后仍失败？→ 标记 needs-human-review
```

### 3.3 场景二：应用版本更新

版本监控不依赖自身轮询。anitya 持续追踪上游 Release，检测到新版本后通过 webhook 触发 `version-update` workflow。同时也支持 `workflow_dispatch` 手动触发。

```
anitya 检测到上游新版本
  → webhook → version-update workflow
    → 以 doc/image-info.yml 的 name 为 key 查 projectsInfoUrl
    → 调 projectsInfoUrl 获取版本详情（app_up vs app_openeuler 对比）
      → 确认需更新？
        → 两个对抗对并行：
          ┌ 对抗对一：Creator 复制已有版本目录树 → 替换版本字符串 → 生成新 Dockerfile + 更新 meta.yml + README.md
          └ 对抗对二：Testcase Creator 复用已有测试套件，适配新版本
        → 双架构构建 + 测试
          → 通过？→ 提 PR（去重：已有同名 PR 则跳过）
          → 失败？→ Fixer 分析 + 修复（最多 3 轮）
```

### 3.4 场景三：openEuler 大版本升级

此场景直接借鉴 easysoftware 的 `batchUpdatePremiumApp()` 模式：遍历所有 OS 版本，对缺少最新镜像的版本生成 Dockerfile。

```
检测到新 oe 版本
  → 枚举所有应用的 meta.yml，生成待更新应用清单
    → Matrix Strategy 并行：每个应用占用 1 组 Runner 对（x86 + ARM64）
      → 复制已有版本的 Dockerfile 目录树 → 替换 ARG BASE 中的 oe 版本字符串
      → 追加 meta.yml 新 tag 条目
      → 构建（双架构）→ 运行已有测试套件
        → 通过？→ 提 PR（或汇总到批量 PR）
        → 失败？→ Fixer 分析日志 + 修复（3 轮）
          → 仍失败？→ 生成失败报告，该应用标记 skip
    → 汇总：成功应用 → 批量 PR；失败应用 → 单独 Issue
```

**并行模式：** 多个镜像同时在各自 Runner 对上构建，并发上限由可用 self-hosted Runner 数量决定，超出自动排队。`fail-fast: false` 保证单镜像失败不影响其他。此并行策略与 easysoftware 的串行遍历不同——easysoftware 作为单一 Java 进程无需并行，而本系统的 GitHub Actions Matrix 天然适合并行化。

---

## 4. Agent 协作架构

### 4.1 角色定义

系统共 5 个 Agent 角色。核心设计模式：每个"生成者"配一个"QA 挑战者"构成对抗对。CI Failure Analyst 不设为独立角色，其诊断能力作为 Fixer 的内置能力通过知识库驱动。

#### 对抗对一：Image Creator + Image QA

| | Image Creator（生成者） | Image QA（挑战者） |
|------|------|------|
| 职责 | 根据应用信息生成完整镜像目录 | 审查 Creator 的输出，挑战其正确性与合规性 |
| 输入 | package_name, source_repo_url, domain, os_version, os_tag | Creator 的全部输出：Dockerfile、meta.yml、README.md、doc/、logo |
| 输出 | Dockerfile + meta.yml + README.md + doc/image-info.yml + logo | 审查报告（问题清单 + 严重程度 + 修改建议） |
| 对抗方式 | 根据 QA 反馈修正输出 | 从以下角度挑战：依赖是否完整？包名是否正确？端口是否暴露？文档是否符合规范？meta.yml 路径是否匹配？ |
| 禁止 | 修改已有文件 | 直接修改文件（只提问题，由 Creator 修改） |

#### 对抗对二：Testcase Creator + Testcase QA

| | Testcase Creator（生成者） | Testcase QA（挑战者） |
|------|------|------|
| 职责 | 独立编写功能测试用例 | 审查测试用例质量，挑战其充分性与有效性 |
| 输入 | package_name, version, dockerfile_path, binary_name, category（不读 Creator 推理链） | Testcase Creator 的全部输出：goss.yaml、goss_wait.yaml、test.sh |
| 输出 | tests/ 目录 + test-ai-result.json | 审查报告（覆盖缺口 + 误报风险 + 遗漏的攻击面） |
| 对抗方式 | 根据 QA 反馈补充测试用例 | 从以下角度挑战：是否覆盖所有攻击面（依赖、端口、权限、启动、边界）？是否有误报风险？是否遗漏关键功能验证？ |
| 禁止 | 读取 Creator 推理链 | 直接修改文件 |

#### Code Fixer（修复者，内置故障分析）

| 属性 | 说明 |
|------|------|
| 职责 | 本地验证失败后介入：分析日志、诊断根因、实施最小化修复 |
| 修复对象 | Dockerfile、meta.yml、README.md、doc/image-info.yml、goss.yaml、test.sh |
| 输入 | 构建日志、测试输出、PR 文件清单（白名单）、fix_branch、故障模式知识库 |
| 输出 | 代码/文档变更 + 修复摘要（含根因分析） |
| 禁止 | 创建新文件、修改白名单外文件、禁用 lint 规则、删除测试 |

### 4.2 对抗模型

对抗不是 Creator 和 Testcase Creator 之间互相对抗，而是**每个生成者都有一个 QA 来挑战它**。两个对抗对内部先辩论修正，通过后再进入本地验证。Fixer 仅在本验证失败后介入。

```
                         ┌──────────────────┐
                         │   Orchestrator   │
                         │   (确定性代码)    │
                         └────────┬─────────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           │                      │                      │
           ▼                      ▼                      │
  ┌─────────────────┐    ┌─────────────────┐             │
  │  对抗对一（并行） │    │  对抗对二（并行） │             │
  │                 │    │                 │             │
  │ ┌─────────────┐ │    │ ┌─────────────┐ │             │
  │ │Image Creator│ │    │ │  Testcase   │ │             │
  │ │  (生成)     │ │    │ │  Creator    │ │             │
  │ └──────┬──────┘ │    │ │  (生成)     │ │             │
  │        │        │    │ └──────┬──────┘ │             │
  │        ▼        │    │        │        │             │
  │ ┌─────────────┐ │    │        ▼        │             │
  │ │  Image QA   │ │    │ ┌─────────────┐ │             │
  │ │  (挑战)     │ │    │ │ Testcase QA │ │             │
  │ └──────┬──────┘ │    │ │  (挑战)     │ │             │
  │        │        │    │ └──────┬──────┘ │             │
  │        │ 有问   │    │        │ 有问   │             │
  │        │ 题？   │    │        │ 题？   │             │
  │        ▼        │    │        ▼        │             │
  │  Creator 修正   │    │  Creator 补充   │             │
  │  (最多 2 轮)   │    │  (最多 2 轮)   │             │
  │        │        │    │        │        │             │
  │        ▼        │    │        ▼        │             │
  │   QA 认可       │    │   QA 认可       │             │
  └────────┬────────┘    └────────┬────────┘             │
           │                      │                      │
           └──────────────────────┼──────────────────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │   本地验证         │
                         │  build + 测试     │
                         └────────┬─────────┘
                                  │
                        ┌─────────┴─────────┐
                        ▼                   ▼
                     通过                 失败
                        │                   │
                        ▼                   ▼
                 ┌──────────┐       ┌──────────────┐
                 │  提交 PR  │       │    Fixer     │
                 │ (附两对    │       │  (分析+修复)   │
                 │  对抗记录) │       └──────┬───────┘
                 └──────────┘              │
                                           │ ← ← ← loop（最多 3 轮）← ← ←
                                           ▼ (3 轮后仍失败)
                                    ┌──────────────────┐
                                    │ needs-human      │
                                    │   -review        │
                                    │ 附完整对抗+修复记录│
                                    └──────────────────┘
```

**对抗的本质：**

Image QA 和 Testcase QA 各自与配对的 Creator 形成**开发-QA 对抗关系**。QA 不直接动手改，而是提出问题迫使 Creator 修正。这与传统 CI（所有人都同意后再检查）的区别在于：对抗对内的辩论发生在本地验证之前，输出的质量已经被 QA 挑战过了，本地验证是用来裁决的。QA 赢了（发现问题）说明 Creator 需要修正；Creator 修正到 QA 无话可说，才进入本地验证。

### 4.3 收敛策略

系统有两层收敛控制：

**第一层：对抗对内 QA 轮次（最多 2 轮）**
```
QA 提出问题 → Creator 修正 → QA 再次审查
  → 认可？→ 通过，进入本地验证
  → 仍有问题？→ Creator 再修正（第 2 轮）
    → 2 轮后 QA 仍不认可？→ 记录分歧，标注在 PR 中，仍进入本地验证
```
QA 不认可不代表阻塞——对抗对的任务是尽可能提高质量，但最终裁决权在本地验证。

**第二层：Fixer 修复轮次（最多 3 轮）**
```
第 1 次本地验证失败 → Fixer 分析 + 修复 → 重新验证
第 2 次本地验证失败 → Fixer 分析 + 修复 → 重新验证
第 3 次本地验证失败 → 标记 needs-human-review，附完整诊断记录
```
- Fixer 参考 `docs/failure-patterns.md` 历史故障模式
- 日志不足时标注 "insufficient evidence"，不得猜测

### 4.4 置信度评分

| 因子 | 权重 | 衡量方式 |
|------|------|---------|
| 构建成功（双架构） | 0.35 | x86_64 + ARM64 build exit code = 0 |
| 测试通过率 | 0.30 | 双架构均通过 |
| Linter 合规 | 0.20 | Hadolint 违规数 |
| 元数据一致性 | 0.15 | meta.yml 与实际文件匹配 |

阈值：≥ 0.85 自动合入就绪；0.75~0.85 自动提 PR 需 Review；< 0.75 退回循环

---

## 5. 测试策略

### 5.1 核心决策：每应用共享测试套件

**推荐：每个应用一套共享测试套件，对所有版本执行。**

| 维度 | 共享套件（推荐） | 按版本独立 |
|------|-----------------|-----------|
| 维护成本 | 一处修复，所有版本受益 | N 份拷贝需同步 |
| 公众可信性 | 同一标准，公平比较 | 版本特化易被质疑 |
| 新版本成本 | 零新增测试代码 | 需复制整套 |

### 5.2 测试分层

```
第 3 层：运行时测试 (Goss/dgoss)
  · 端口监听  · 进程运行  · HTTP 端点  · 文件内容断言

第 2 层：静态分析 (Hadolint + Dockle)
  · Dockerfile 最佳实践  · 镜像标签合规

第 1 层：预构建检查
  · meta.yml schema  · "只能新增" 差分检查
```

> openEuler 目前不支持 Trivy CVE 扫描，未纳入测试流水线。

### 5.3 测试用例与结果归档目录设计

**现状问题：** 目标仓库当前将测试用例放在顶层 `tests/<app-name>/` 目录下，与应用的 Dockerfile 目录分离。例如 `tests/rust/` 和 `Bigdata/kylin/` 分属两处，测试用例和被测对象之间没有目录关联。

**设计方案：测试用例与 Dockerfile 同目录，测试结果归档在应用目录内。**

```
openeuler-docker-images/
├── Bigdata/
│   └── kylin/
│       ├── README.md
│       ├── meta.yml
│       ├── doc/
│       │   └── image-info.yml
│       ├── tests/                         # 应用级共享测试用例
│       │   ├── goss.yaml                  # 运行时断言
│       │   ├── goss_wait.yaml             # 就绪等待条件
│       │   └── test_helpers.sh            # 辅助函数
│       ├── results/                       # 测试结果归档
│       │   └── 5.0.3/                     # 按应用版本归档
│       │       └── 24.03-lts-sp4/         # 按 oe 版本归档
│       │           ├── x86_64.junit.xml   # x86_64 测试结果
│       │           ├── aarch64.junit.xml  # ARM64 测试结果
│       │           └── build.log          # 构建日志
│       └── 5.0.3/                         # 应用版本
│           └── 24.03-lts-sp4/             # oe 版本
│               ├── Dockerfile
│               └── test.sh                # CI 入口（调用 tests/ 下的共享用例）
└── tests/                                 # 废弃（不再使用）
```

**关键设计决策：**

| 决策 | 说明 |
|------|------|
| 测试用例放在 `<app>/tests/` | 与 Dockerfile 同目录，应用级共享，所有版本复用 |
| 测试结果放在 `<app>/results/` | 按 `<app-ver>/<oe-ver>/` 双层归档，与 Dockerfile 路径一一对应 |
| 每个 Dockerfile 同级有 `test.sh` | 轻量入口，负责调用 `tests/` 下的共享用例，传入版本参数 |
| 结果按架构独立存储 | `x86_64.junit.xml` 和 `aarch64.junit.xml`，一一对应，一个架构通过不代表另一个 |
| 不修改顶层 `tests/` 目录 | 已有内容保留不动，新应用不再使用顶层目录 |

**与当前仓库的对比：**

| 维度 | 当前仓库（`tests/<app>/`） | 本设计（`<app>/tests/` + `<app>/results/`） |
|------|--------------------------|---------------------------------------------|
| 测试用例位置 | 顶层 `tests/`，与 Dockerfile 分离 | 应用目录内，与 Dockerfile 同属一个目录树 |
| 结果归档 | `tests/<app>/results/<version>/` | `<app>/results/<app-ver>/<oe-ver>/`，与 Dockerfile 路径一一对应 |
| 可发现性 | 需知道顶层有 `tests/` 目录 | 进入应用目录即可看到测试和结果 |
| 版本对应 | 结果只按应用版本归档 | 结果按应用版本 + oe 版本双层归档，精确对应每个 Dockerfile |

### 5.4 公众可信性

可信性来自对抗而非自证：

1. **QA 审查记录公开**：PR 内嵌 Image QA 和 Testcase QA 的审查报告——生成者的输出被独立角色挑战过，不是自我证明
2. **对抗分歧透明**：QA 不认可的问题标注在 PR 中，即使通过本地验证也保留分歧记录——任何人都能看到 QA 发现了什么
3. **JUnit XML 归档**：测试结果以机器可解析格式作为 Artifact 归档（不可变、带时间戳、绑定构建 SHA）
4. **双架构独立**：x86_64 和 ARM64 各自独立记录，一个架构通过不代表另一个通过

---

## 6. 构建流水线

### 6.1 并行模型

每个镜像包含 x86_64 和 ARM64 两个架构。系统的并行有双层含义：

1. **同一镜像内**：x86_64 和 ARM64 两个 Job 并行执行（一个镜像的必选项）
2. **不同镜像间**：多个镜像的构建并行执行，每个镜像占用 1 组 Runner 对

```
批量 N 个镜像并行构建示意：

镜像 A:                          镜像 B:                          镜像 C:
build-arm64 [ARM64 Runner]       build-arm64 [ARM64 Runner]       build-arm64 [ARM64 Runner]
     │ 并行                            │ 并行                            │ 并行
build-amd64 [x86_64 Runner]      build-amd64 [x86_64 Runner]      build-amd64 [x86_64 Runner]
     │                                │                                │
     ▼                                ▼                                ▼
manifest merge                   manifest merge                   manifest merge

     ↑ 所有镜像同时执行 ↑（并发上限 = 可用 Runner 对数）
```

| 场景 | 并行粒度 | 资源 |
|------|---------|------|
| 新增单个镜像 | 1 镜像 × 2 架构 | x86_64×1 + ARM64×1 |
| N 个应用版本更新 | N 镜像并行 | x86_64×N + ARM64×N |
| M 个应用 oe 升级 | M 镜像并行 | x86_64×M + ARM64×M |

### 6.2 单个镜像构建流程

```
Phase 1: 双架构原生构建（无 QEMU，各跑在原生硬件上）
  Job build-amd64 [self-hosted, Linux, x64]:
    docker buildx build --platform linux/amd64 --output type=image,push-by-digest=true
  Job build-arm64 [self-hosted, Linux, ARM64]:
    docker buildx build --platform linux/arm64 --output type=image,push-by-digest=true

Phase 2: Manifest 合并
  docker buildx imagetools create -t openeuler/<app>:<tag> \
    <registry>/<app>@<amd64-digest> \
    <registry>/<app>@<arm64-digest>
```

- `fail-fast: false` — 单架构失败不取消另一架构
- `push-by-digest=true` — 按 digest 推送，不直接打 tag
- 每架构独立 `type=registry` cache scope，避免并行碰撞

### 6.3 多阶段 Dockerfile

```dockerfile
# Stage 1: 依赖层（鲜少变化，利用缓存）
FROM openeuler/openeuler:24.03-lts AS deps
RUN --mount=type=cache,target=/var/cache/yum \
    yum install -y <build-tools> && yum clean all

# Stage 2: 构建
FROM deps AS builder
COPY src/ /build/
WORKDIR /build
RUN make

# Stage 3: 最小运行时
FROM openeuler/openeuler:24.03-lts
COPY --from=builder /build/output /usr/local/bin/app
ENTRYPOINT ["/usr/local/bin/app"]
```

---

## 7. 版本管理

### 7.1 上游版本监控

版本监控全部委托给 anitya，系统不自行轮询。

```
anitya 检测到上游新版本
  → webhook 触发 version-update workflow
    → 读取 doc/image-info.yml 的 name 字段
      → 调 projectsInfoUrl + name 获取版本对比详情（app_up vs app_openeuler）
        → 确认需要更新 → 启动 Creator 对抗对 → 构建验证 → 提 PR
```

应用名即是 anitya 中的项目查找 key（与 easysoftware 的 `projectsInfoUrl + appName` 一致）。新应用创建时若 anitya 中不存在，Creator Agent 用 `upstream` 中的 `backend` 和 `homepage` 完成注册。

### 7.2 openEuler 版本监控

- 监控 `https://repo.openeuler.org/` 新版本目录
- 版本模式：`{YY}.{MM}-lts`、`{YY}.{MM}-lts-sp{N}`

### 7.3 命名约定

| 层级 | 格式 | 示例 |
|------|------|------|
| 应用版本目录 | 上游版本号 | `1.27.2/` |
| oe 版本目录 | oe 官方版本名 | `24.03-lts/` |
| 复合版本 | 软件栈组合 | `2.1.0-cann7.0.RC1.alpha002/` |
| Tag | `{app-ver}-{oe-ver-short}` | `3.3.1-oe2203lts` |

---

## 8. PR 自动化规范

### 8.1 PR 必含内容

1. **Dockerfile** — 仅新增
2. **更新的 meta.yml** — 追加新 Tag
3. **更新的 README.md** — 追加版本行
4. **doc/** — 新应用时生成
5. **测试文件** — 新应用时生成
6. **构建证明** — 折叠 `<details>`，双架构独立日志 + SHA256 digest
7. **测试结果** — JUnit XML，双架构独立展示
8. **差分摘要** — 机器生成的文件变更列表
9. **Agent 声明** — 生成者 + 置信度

### 8.2 PR 模板

```markdown
## Automated PR: {app-name} {new-version} on {oe-version}

### Changes
- Added `{app-name}/{version}/{oe-version}/Dockerfile`
- Updated `{app-name}/meta.yml` (appended tag)
- Updated `{app-name}/README.md` (appended version row)

### Build Proof
<details>
<summary>x86_64 build (exited 0, digest: sha256:...)</summary>

```
... full build output ...
```
</details>

<details>
<summary>ARM64 build (exited 0, digest: sha256:...)</summary>

```
... full build output ...
```
</details>

### Adversarial Review Records

<details>
<summary>对抗对一：Image Creator ↔ Image QA</summary>

| 角色 | 结论 |
|------|------|
| Image Creator | 生成完成 (confidence: 0.92) |
| Image QA | 审查通过，发现 X 个问题已修正，Y 个建议 |

QA 审查角度：依赖完整性 ✓、包名正确性 ✓、端口暴露 ✓、文档合规 ✓、meta.yml 路径匹配 ✓

</details>

<details>
<summary>对抗对二：Testcase Creator ↔ Testcase QA</summary>

| 角色 | 结论 |
|------|------|
| Testcase Creator | 生成 N 个测试用例 (confidence: 0.88) |
| Testcase QA | 审查通过，补充 X 个覆盖缺口，修正 Y 个误报 |

QA 审查角度：攻击面覆盖 ✓、误报风险 ✓、关键功能验证 ✓、边界条件 ✓

</details>

### Test Results
<details>
<summary>x86_64 — all assertions passed</summary>

```
... JUnit XML ...
```
</details>

<details>
<summary>ARM64 — all assertions passed</summary>

```
... JUnit XML ...
```
</details>

### Generated By
- Image Creator + Image QA (对抗对一)
- Testcase Creator + Testcase QA (对抗对二)
```

---

## 9. 技术选型

### 9.1 技术栈

| 层次 | 技术 | 说明 |
|------|------|------|
| CI/CD 平台 | GitHub Actions | 系统约束 |
| Runner | Self-hosted VMs (x86 + ARM64) | 多组云上虚拟机 |
| 确定性代码 | Python 3 | 主流程编排 + API 封装 |
| Agent 运行时 | Claude Agent SDK | 不确定性决策 |
| 容器构建 | Docker BuildKit + buildx | 多架构原生构建 |
| 测试框架 | Goss/dgoss + shUnit2 | 声明式运行时验证 |
| Lint | Hadolint + Dockle | Dockerfile + 镜像检查 |
| Git 平台 | GitCode（主） | 基于 Gitea `/api/v5`，认证用 `PRIVATE-TOKEN` 头（非 GitHub API v3） |
| 版本监控 | anitya（通过 `projectsInfoUrl` 查询） | 上游版本跟踪全部委托给 anitya，系统响应 webhook |

### 9.2 关键约束

| 约束 | 应对 |
|------|------|
| GitCode 文件 API 只读 | Git 操作（clone → modify → commit → push） |
| GitCode API 50 req/min | 合并请求 + 指数退避重试 |
| anitya webhook 因故漏触发 | `workflow_dispatch` 手动兜底 + idempotency gate |
| Self-hosted Runner 数量有限 | Matrix 限流 + 队列管理 |
| 只能新增不能修改 | meta.yml 索引 + 本地差分检查 |

---

## 10. 风险与缓解

| 风险 | 概率 | 缓解 |
|------|------|------|
| Agent 生成的 Dockerfile 无法构建 | 高 | 确定性构建验证 + Fixer 3 轮（内置故障知识库） |
| anitya 监控服务不可用 | 低 | openEuler 自建的 anitya 实例基于 Fedora anitya（多年生产验证）；有 `backupProjectsInfoUrl` 灾备 |
| 批量 oe 升级时资源耗尽 | 中 | Matrix 限流 + fail-fast: false |
| Agent 幻觉生成错误元数据 | 中 | CI schema 验证 + Testcase Creator 独立检查 |
| 双架构构建结果不一致 | 低 | 每架构独立测试，全部通过才合并 manifest |
| 知识库过时 | 低 | Fixer 成功修复后自动回写 |

---

## 11. 项目结构

```
openeuler-docker-images-workflow/
├── .github/
│   ├── workflows/
│   │   ├── new-image.yml              # 场景一：新镜像请求
│   │   ├── version-update.yml         # 场景二：应用版本更新（anitya webhook 或 workflow_dispatch 触发）
│   │   ├── oe-upgrade.yml             # 场景三：openEuler 大版本升级
│   │   └── verify.yml                  # 本地验证（构建 + 测试 + 差分）
│   └── agents/
│       ├── image-creator.md           # Image Creator（生成者）
│       ├── image-qa.md                # Image QA（挑战者）
│       ├── testcase-creator.md        # Testcase Creator（生成者）
│       ├── testcase-qa.md             # Testcase QA（挑战者）
│       └── code-fixer.md              # Fixer（修复者，内置故障分析）
├── scripts/
│   ├── harness/
│   │   ├── parse_issue.py             # Issue 解析
│   │   ├── query_version.py           # 调 projectsInfoUrl 获取版本对比
│   │   ├── validate_meta.py           # meta.yml schema 验证
│   │   ├── gate_diff.py               # "只能新增" 差分检查
│   │   ├── compose_pr.py              # PR 内容组装
│   │   └── run.py                     # 主编排入口
│   └── utils/
│       ├── gitcode.py                 # GitCode API 封装
│       ├── scoring.py                 # 置信度计算
│       └── artifacts.py               # 测试结果归档
├── docs/
│   ├── DESIGN.md                      # 本文档
│   └── failure-patterns.md            # 故障模式知识库
├── templates/
│   ├── new-image-issue.md             # Issue 模板
│   ├── pr.md                          # PR 模板
│   └── test/
│       ├── goss.yaml.j2
│       └── test.sh.j2
├── tests/
│   ├── test_parse_issue.py
│   ├── test_gate_diff.py
│   └── test_validate_meta.py
├── README.md
└── REQUIREMENTS.md                     # openeuler-images-requirements.md
```
