# Windows 部署与精简镜像编译说明

本文是 [容器配置文档说明](./容器配置文档说明.md) 的 **Windows 版补充**：在 Windows 上用 Docker Desktop（**无需单独安装 Ubuntu 虚拟机**）完成 `robocup-sim:team-v1-slim` 精简镜像的导入、容器替换、重新编译与启动。

精简镜像已删除编译产物，导入并由 Studio 激活后**必须重新编译**；而在 Windows/WSL2 下重新编译会额外遇到三个坑（SDK 路径未配置、编译吃光内存卡死、team_id 不匹配），本文重点解决这三个问题。

2026.11.14

## 0. 前置条件

- **Docker Desktop**（Windows 版，用 WSL2 后端跑 Linux 容器，不需要单独装 Ubuntu）
- **Booster Studio**（创建 / 激活虚拟机器人）
- **Python 3**（Windows 上命令是 `py`；替换脚本需要）
- 镜像文件：`robocup-sim-team-slim.tar.gz` 及其 `.sha256`（下载见 [镜像下载说明](./镜像下载说明.md)）
- `replace_robot_container.py`（Windows 版见本仓库根目录，下同）

## 1. 开启 Docker Desktop 的 TCP 2375（Windows 特有）

`replace_robot_container.py` 默认走 Linux 的 unix socket，Windows 下需要让 Docker 暴露 TCP 接口：

1. 打开 Docker Desktop → **Settings → General**
2. 勾选 **"Expose daemon on tcp://localhost:2375 without TLS"**
3. Apply & Restart

验证：

```bash
curl http://localhost:2375/_ping
```

返回 `OK` 即可。

> Windows 版的 `replace_robot_container.py` 已把连接方式从 unix socket 改为 `tcp://localhost:2375`，直接用本仓库根目录的脚本即可。

## 2. 校验并导入镜像

与 Linux 教程相同，在镜像文件所在目录打开 **Git Bash**（Windows 自带的 cmd / PowerShell 没有 `sha256sum`）：

```bash
sha256sum -c robocup-sim-team-slim.tar.gz.sha256
docker image load -i robocup-sim-team-slim.tar.gz
```

正常显示 `Loaded image: robocup-sim:team-v1-slim`。

## 3. 创建 / 激活机器人并找到容器名

在 Booster Studio 创建并激活一个虚拟机器人，然后在终端找到它对应的容器名：

```bash
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}'
```

记下形如 `xxxxx-k1` 的容器名。

## 4. 检查兼容性 & 替换容器

与 Linux 教程相同，注意 Windows 上把 `python3` 换成 `py`：

```bash
py replace_robot_container.py check --target <容器名> --image robocup-sim:team-v1-slim
```

在 Booster Studio 中停用该机器人（**不删除**机器人记录），确认容器已停止后执行替换：

```bash
py replace_robot_container.py replace --target <容器名> --image robocup-sim:team-v1-slim --backup-dir ./migration-backups
```

替换完成后，在 Booster Studio 重新激活该机器人。

## 5. 重新编译（Windows/WSL2 关键）

精简镜像已删除编译产物，需要重新编译。**直接跑 `build_no_cuda.sh` 会报错或卡死**，必须先做下面两步。

### 5.1 补 SDK 头文件与库的搜索路径（精简镜像漏配了）

镜像里的 Booster SDK 头文件在 `/opt/booster/python3/site-packages/include/`，库在 `/opt/booster/python3/site-packages/lib/`，但都没加进编译器的搜索路径。进入容器后执行：

```bash
docker exec -it <容器名> bash

# 头文件
ln -s /opt/booster/python3/site-packages/include/booster          /usr/local/include/booster
ln -s /opt/booster/python3/site-packages/include/booster_internal  /usr/local/include/booster_internal
ln -s /opt/booster/python3/site-packages/include/booster_fastdds   /usr/local/include/booster_fastdds

# 库
ln -s /opt/booster/python3/site-packages/lib/libbooster_robotics_sdk.a /usr/local/lib/libbooster_robotics_sdk.a
```

> 不补会报 `fatal error: booster/robot/b1/b1_api_const.hpp: No such file or directory` 和 `ld: cannot find -lbooster_robotics_sdk`。

### 5.2 限制并行度，避免内存耗尽卡死

默认 `colcon build` 会开满所有 CPU 核心并行编译，16G 内存的机器会被几十个 g++ 瞬间吃光，Docker 引擎直接卡死（连 `docker ps` 都报 500）。必须限到 **2 个并行任务、一次只编一个包**：

```bash
cd /root/robocup_demo
MAKEFLAGS="-j2" colcon build --symlink-install --base-paths src --executor sequential --cmake-args -DNO_CUDA=ON
```

6 个包全部 `Finished` 即编译成功。如果 16G 机器还是卡，把 `-j2` 换成 `-j1`。

> 上面命令里的 `-DNO_CUDA=ON` 是让 vision 用 CPU/ONNX 推理（仿真必须）。原脚本还带 `-DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_CXX_FLAGS="-g -rdynamic"` 等调试参数，属于可选项，省略不影响运行。

## 6. 配置 team_id

`src/brain/config/config.yaml` 里的 `team_id` 必须和比赛里的队号一致，否则 brain 会丢弃裁判消息、机器人不动。默认是 `29`，一般比赛两队是 `1` 和 `2`：

```bash
sed -i 's/team_id: 29/team_id: 1/' /root/robocup_demo/src/brain/config/config.yaml
```

把 `1` 换成你机器人在比赛里的队号。改完重启生效。

## 7. 启动机器人

```bash
cd /root/robocup_demo
./scripts/sim_start.sh
```

`vision` / `brain` / `game_controller` 三个节点会在后台启动。验证：

```bash
ps aux | grep -E "vision_node|brain_node|game_controller" | grep -v grep
tail -f brain.log   # 或 vision.log / game_controller.log
```

三个进程都在、日志无报错即成功。回到 Booster Studio 开始比赛，裁判状态进入 PLAYING 后机器人开始行动。

## 常见坑速查

| 现象 | 原因 | 解决 |
|---|---|---|
| `booster/robot/b1/b1_api_const.hpp: No such file or directory` | SDK 头文件路径未配置 | 见 5.1 建软链接 |
| `ld: cannot find -lbooster_robotics_sdk` | SDK 库路径未配置 | 见 5.1 建软链接 |
| 编译卡死、`docker ps` 报 500 Internal Server Error | 并行编译吃光内存 | 见 5.2 用 `MAKEFLAGS=-j2 --executor sequential` |
| brain 日志 `received invalid game controller message ... teamId 29` | team_id 与比赛不匹配 | 见 6 改 `config.yaml` |
| 机器人不动、日志一直 `SelfLocateEnterField` | 比赛未开球，或 team_id 错 | 确认比赛已进入 PLAYING，且 team_id 正确 |
