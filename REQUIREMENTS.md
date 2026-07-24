# 主题
设计 openEuler 容器镜像自动创建、更新、维护的系统

# 背景和目标

## 背景

openEuler 社区官方维护了一批应用容器镜像的 Dockerfile 和相关的文档，在**上游应用版本更新时**、**需要增加一个全新的应用镜像时**、**openEuler新版本发布**时均需要自动更新或创建对应的镜像

### 容器镜像分类
openEuler社区有如下两种分类：
- 基础容器镜像：上文中提到的官方发布到 https://repo.openeuler.org/openEuler-{VERSION}/docker_img/ 的容器镜像均为基础镜像，基础镜像仅包含少量的基础软件。
- 应用容器镜像：在基础镜像之上，安装特定场景应用软件的镜像，例如nginx，或AI软件栈的容器镜像等。

### 系统部署约束
这个 openEuler 容器镜像系统必须通过 Github Action 部署，这意味着：
- 整体的运行是通过 workflow 来承载
- 软件版本的更新或者 openEuler 版本的更新，通常会带来大批量应用镜像的更新，需要考虑并行执行
- 硬件资源能给到的是多组 self-hosted 的云上虚拟机，包含 x86 和 ARM64 架构

## 目标

1. 用户可以根据Issue描述需求，新增应用镜像，我希望能够自动提交一个PR来完成，创建一个新的镜像目录，并且符合仓库README.md约束的规范存放、生成所有要求的文档等
2. 当某个应用已存在应用容器镜像，且应用上游发布了新的版本，我希望你能提交 PR 来完成应用新版本的镜像发布，同上，也需要符合仓库规范，只能按规则新增一个目录，而不是修改已有镜像目录文件
3. 当 openEuler 发布新版本时，我希望你能够基于新的 openEuler 版本更新所有已有的应用容器镜像，你只能按规则新增一个目录，而不是修改已有镜像目录文件

## 要求

1. 所有目标的达成不需要人工介入，因此要求：

- 不确定性的内容可通过agent处理，可能存在 creator, fixer 等互相协作或对抗的 agent 角色
- 确定性的内容尽量使用代码实现，保证 harness，如构建、测试等环节
- 系统的主流程必须要确定性代码来承载

2. 在新增一个应用镜像的时候，必须提供基础的功能测试用例，这个测试用例必须具备公众可信性，至于每个镜像的所有版本共享一组测试用例还是每个版本独有一组测试，需要你深度研究下

3. 无论是新增还是更新镜像的场景，提交 PR 前必须保证 Dockerfile 是可以构建出镜像，而且必须通过基础的功能测试并在 PR 内包含测试结果，测试结果的归档可参考：https://gitcode.com/openeuler/openeuler-docker-images/tree/master/tests/rust ， 但是我认为这种测试用例以及Dockerfile的归档目录不是很建议，你可以深度研究下给出合理的方案

# 参考信息

1. openEuler 官方镜像仓库：https://gitcode.com/openeuler/openeuler-docker-images

2. 一个完整的镜像目录参考：https://gitcode.com/openeuler/openeuler-docker-images/tree/master/Bigdata/kylin

3. 一些实验过的 agent 角色：

- Image Creator : https://github.com/opensourceways/openeuler-docker-autopilot/blob/main/.github/agents/image-creator.md
- Dockerfile Fixer : https://github.com/opensourceways/openeuler-docker-autopilot/blob/main/.github/agents/code-fixer.md
- Failure Analyst : https://github.com/opensourceways/openeuler-docker-autopilot/blob/main/.github/agents/ci-failure-analyst.md
- 测试用例生成器：https://github.com/Tian-Fantasea/OPENEULER-DOCKER-AUTOPILOT/blob/main/.github/agents/image-tester.md

4. Gitcode API 文档：https://docs.gitcode.com/docs/apis/

5. openEuler 应用容器镜像升级：https://github.com/opensourceways/easysoftware-autoupgrade/tree/release/bugfix-appupdate

6. openEuler 应用的上游版本监控：https://easysoftware-monitoring.test.osinfra.cn/ ， 以及源码实现：https://github.com/opensourceways/anitya ， 其中 easysoftware-autoupgrade 的版本监控调的就是这个服务