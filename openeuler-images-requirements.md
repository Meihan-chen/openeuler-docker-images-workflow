# 主题
设计openEuler容器镜像自动创建、更新、维护系统

# 背景和目标

## 背景

openEuler社区官方维护了一批应用容器镜像的Dockerfile和相关的文档（仓库是：https://gitcode.com/openeuler/openeuler-docker-images），在上游应用更新时、需要新增一个镜像时、openEuler新版本发布时均需要自动更新或创建对应的镜像

关于容器镜像，openEuler社区有如下两种分类：

- 基础容器镜像：上文中提到的官方发布到 https://repo.openeuler.org/openEuler-{VERSION}/docker_img/ 的容器镜像均为基础镜像，基础镜像仅包含少量的基础软件。

- 应用容器镜像：在基础镜像之上，安装特定场景应用软件的镜像，例如包含nginx，或AI软件栈的容器镜像等。

## 目标

1. 用户可以根据Issue描述需求，新增应用镜像，我希望能够自动提交一个PR来完成，创建一个新的镜像目录，并且符合仓库README.md约束的规范存放、生成所有要求的文档等

2. 当某个应用已存在应用容器镜像，且应用上游发布了新的版本，我希望你能提交PR来完成应用新版本的镜像发布，同上，也需要符合仓库规范，只能按规则新增一个目录，而不是修改已有镜像目录文件

3. 当openEuler发布新版本时，我希望你能够基于新的openEuler版本更新所有已有的应用容器镜像，你只能按规则新增一个目录，而不是修改已有镜像目录文件

## 要求

0. 所有目标的达成不需要人工介入，不确定性的内容可通过agent处理，确定性的内容尽量使用代码实现，保证harness

1. 以上三个目标尽可能使用相同的核心逻辑，仅在任务入口做区分

2. 新增一个应用镜像的时候，必须提供基础的功能测试用例，这个测试用例必须具备公众可信性，至于每个镜像的所有版本共享一组测试用例还是每个版本独有一组测试，需要你深度研究下

3. 在提交PR前，必须通过基础的功能测试并在PR内包含测试结果，测试结果的归档可参考：https://gitcode.com/openeuler/openeuler-docker-images/tree/master/tests/rust，https://gitcode.com/openeuler/openeuler-docker-images/tree/master/tests/rust 但是我认为这种测试用例以及Dockerfile的归档目录不是很建议，你可以深度研究下给出合理的方案

# 参考信息

1. 一个完整的镜像目录参考：https://gitcode.com/openeuler/openeuler-docker-images/tree/master/Bigdata/kylin
