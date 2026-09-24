# 全容器部署指南

[English](CONTAINER_DEPLOYMENT.en.md)

本文适用于 Synology Container Manager，以及支持 Docker Compose v2 的普通 Linux、树莓派和
其他 NAS。全容器部署由三个基础服务和每条启用线路的一个 Engine 组成，常驻数量为 `3 + N`：

| 服务 | 用途 |
| --- | --- |
| Control | Web 管理界面、API、数据、线路配置和 Engine 编排 |
| Hardware | 私有 D-Bus、ModemManager、NetworkManager、pcscd、USB 与 SIM bridge |
| Egress | 按国家提供独立 SOCKS5 TCP/UDP 出口 |
| Engine | 每条线路独立运行 SWu、IMS、通话和短信；由 Control 动态创建 |

Compose 只声明前三个基础服务。不要手工为每张 SIM 复制 Engine 服务。

## 1. 发布版本与准备条件

只使用 GitHub Release 附带的 `mdd-sim-gateway-compose-vX.Y.Z.yaml`。该文件中的四个 GHCR
镜像已经固定为相同版本，不使用 `latest`。如果某个 Release 没有这个文件，它就不支持正式
全容器安装。

部署前准备：

- Docker Engine 和 Docker Compose v2；群晖使用官方 Container Manager；
- amd64 或 arm64 主机；
- 至少 6 GiB 可用空间，以便首次拉取和以后保留一代回滚镜像；
- 一个只用于本项目的数据目录；群晖示例为 `/volume1/docker/mdd-sim-gateway`；
- 一个未占用的 HTTPS 管理端口，默认 `10443`；
- NAS 的固定 LAN 地址或局域网 DNS 名称；
- USB 设备能出现在宿主 `/dev/bus/usb`。蜂窝模块还应生成串口、QMI/MBIM 和网卡节点。

不要先在宿主安装 pcscd、ModemManager 或 NetworkManager。全容器模式由 Hardware 容器管理
这些用户态服务，宿主同类服务可能抢占 USB 设备。

## 2. 判断是否需要 NAS 驱动

先插入 USB 模块或读卡器。普通 Linux/Pi 和部分 NAS 的内核已经包含驱动，可以直接部署。
蜂窝模块通常需要看到类似节点：

```text
/dev/ttyUSB0
/dev/cdc-wdm0
/sys/class/net/wwan0
```

普通 PC/SC 读卡器只需要能在 `/dev/bus/usb` 下看到对应 USB 设备，它的 pcscd 和 libccid 在
Hardware 容器内。

如果上面的节点齐全，宿主不需要安装任何项目软件，直接创建 Compose 项目即可。

节点缺失时 Hardware 容器会保持 unhealthy，依赖它的 Control 不会启动。当前版本**不提供**
自动的驱动预检状态：请自己按上面的命令确认缺少哪些节点，再到
[NAS 兼容性与驱动目录](../drivers/README.md)人工比对是否存在完全匹配的记录。

驱动必须同时匹配厂商、型号、CPU 平台、架构、系统完整 build 和内核 release，不得安装相近
型号或相近系统版本的包。目录中当前唯一的记录（DS1621+、DSM 7.4.1-90080、内核 4.4.302+）
状态为 `driver-verified`，其驱动包仍是 `packaging-pending`，**Release 尚未提供任何可安装的
驱动资产**。在出现 `release-ready` 记录之前，不要使用来源不明的 `.ko` 或 `.spk`。

## 3. 在 Synology Container Manager 创建项目

1. 从项目 Release 下载 `mdd-sim-gateway-compose-vX.Y.Z.yaml`，并核对同一 Release 中
   `SHA256SUMS`。也可以在浏览器中打开文件后复制全部内容。
2. 打开 **Container Manager → 项目 → 新增 → 创建 docker-compose.yml**。
3. 项目名称填写 `mdd-sim-gateway`，粘贴 Release 中的 YAML。
4. 将文件开头提示的 `192.168.1.100` 全部替换为这台 NAS 的固定 LAN 地址。
5. 如果共享文件夹不是 `/volume1/docker`，替换 YAML 中的
   `/volume1/docker/mdd-sim-gateway`。不要选择临时目录。
6. 默认映射为 `10443:8443`。如 `10443` 已占用，只修改 `ports` 左侧的宿主端口；容器内
   端口保持 `8443`。
7. 如果代理订阅由 NAS 自身域名提供，把 `MDD_NAS_HOSTNAME` 的默认值改为该域名。它只让
   Egress 在容器内把这个域名解析到 Docker 宿主网关，不修改 NAS DNS 或默认路由。
8. 保存并创建项目。Container Manager 会拉取 Control、Hardware、Egress；Engine 镜像会在
   第一条线路启动时由 Control 使用同一版本拉取。

如果通过 SSH 执行 `docker compose up -d`，容器会出现在“容器”页，但 DSM 的“项目”页不会
自动登记。命令启动后还需要执行一次：

```sh
PROJ=mdd-sim-gateway
UUID=$(cat /proc/sys/kernel/random/uuid)
NOW=$(date -u +"%Y-%m-%dT%H:%M:%S.%6NZ")
CMDIR=/volume1/@appconf/ContainerManager/projects
sudo tee "$CMDIR/${UUID}.config.json" >/dev/null <<EOF
{"created_at":"${NOW}","enable_service_portal":false,"id":"${UUID}","is_package":false,"name":"${PROJ}","service_portal_name":"","service_portal_port":0,"service_portal_protocol":"","services":null,"share_path":"/docker/${PROJ}","state":"","updated_at":"${NOW}","version":2}
EOF
sudo touch "$CMDIR/${UUID}.action.log" "$CMDIR/${UUID}.lock"
sudo chmod 600 "$CMDIR/${UUID}.config.json"
sudo chmod 660 "$CMDIR/${UUID}.action.log"
sudo chmod 444 "$CMDIR/${UUID}.lock"
```

顶层 `name: mdd-sim-gateway`、注册文件中的 `name` 和 `share_path` 必须一致。刷新 DSM 页面后
应能看到项目；若当前 DSM 版本仍缓存旧列表，请在维护窗口重启 Container Manager 套件。
**DSM 7.4 实测套件重启会依次停止并重新启动全部容器项目**，不能在业务时段执行，也不能把它
描述成无中断的界面刷新。通过 DSM 界面新建项目时由 Container Manager 自动完成登记，不需要
手工执行。

不要套用普通 Compose 项目常见的 `chown -R 用户:users` 步骤。MDD 数据目录包含短信、证书、
SIM 配置和通知凭据，必须保持 root 所有和 `0700`；Control/Egress 已移除绕过目录权限的能力，
修改所有者会导致它们无法启动。YAML 的后续编辑应通过 Container Manager 项目界面或 root SSH
完成。

首次拉取需要访问 `ghcr.io`。不能访问时，先从同一 Release 下载本机架构的四个离线镜像包，
核对 `SHA256SUMS` 并在 Container Manager 导入；不得混用不同版本或不同架构的镜像。

## 4. Compose 中必须保留的安全边界

可以修改数据路径、NAS 地址、管理端口和日志大小。不要删除或扩大以下约束：

- Control、Hardware、Egress 的固定容器名和项目归属 label；
- Control 对 Docker socket 的挂载；它只按归属 label 管理本项目 Engine 和服务重启；
- Hardware 的 `/dev`、`/sys/devices`、私有 D-Bus 与 PC/SC 卷；
- Hardware 的 `network_mode: host` 及限定 capability；不要改成 `privileged: true`；
- Engine 内部网络的 `internal: true`；
- Egress 不发布 SOCKS 端口到 NAS；
- `restart: unless-stopped`，用于 NAS 重启后恢复基础服务。

Hardware 中的 NetworkManager 只允许管理 `wwan*` 和 `cdc-wdm*`，蜂窝连接强制
`never-default`。如果它发现任何 NAS 物理网口、Open vSwitch、VLAN、Docker bridge 或
loopback 被接管，会停止蜂窝拨号。国家出口运行在容器网络中，不向 NAS 主路由表安装运营商
路由。

## 5. 首次启动与验收

等待三个基础容器均进入运行或健康状态，然后访问：

```text
https://NAS_LAN_IP:10443/
```

如果修改了宿主端口，则使用修改后的端口访问。首次访问使用自签名证书，
浏览器会要求确认；随后立即创建管理员账号。

按以下顺序验收：

1. “系统设置/诊断”显示 Control、Hardware、Egress 版本完全一致；
2. Hardware 状态为健康，没有宿主 pcscd 抢占，也没有非蜂窝网卡被 NetworkManager 接管；
3. “设备”页只出现实际插入的模块和读卡器；拔插后无需重建项目即可消失、恢复；
4. PC/SC 读卡器只显示 VoWiFi/eSIM 能力，不显示虚假的 4G 开关；
5. 蜂窝模块能读取 SIM 状态，按需开启 4G 后 NAS 默认网关保持不变；
6. 配置国家出口后，UDP 检查通过并显示实际节点名称；
7. 启用 VoWiFi，依次看到 SWu 已连接、USIM 鉴权成功和 IMS 已注册；
8. 浏览器通话页能正常打开，反向代理部署还需确认 WebSocket Upgrade 被转发；
9. 在正式使用前完成一次短信和通话测试。

浏览器软电话信令与 WebUI 同源，不需要额外发布 WSS 端口。Engine 的 RTP 端口从默认
`30000` 开始动态分配；若跨 VLAN 或经过防火墙使用通话功能，需要允许客户端与 NAS 之间的
对应 UDP 流量。

## 6. 数据、备份和证书

所有持久数据位于配置的数据目录，包括设置、数据库、线路状态、证书、通知凭据和更新状态。
不要把该目录放在容器临时层，也不要提交到 Git。

升级和迁移前：

1. 在 WebUI 创建备份；
2. 停止新的配置操作；
3. 备份整个数据目录；
4. 记录当前 Compose Release 文件和镜像 digest。

Control、Hardware 和 Egress 的命名卷保存运行时 socket 或可重建状态，不能替代数据目录备份。
恢复到另一台 NAS 时，先恢复数据目录，再使用与备份版本一致的 Compose 文件启动。

## 7. 更新与回滚

系统设置中的一键更新在全容器模式下由一次性更新助手执行，不增加常驻容器。更新助手会：

1. 下载当前架构的 Control、Hardware、Egress、Engine Release 资产并逐个核对
   `SHA256SUMS`、架构、组件、项目归属和版本；
2. 生成一致的短信/MMS/配置备份，并保存更新前 Compose；
3. 保留用户修改的端口、数据目录、NAS 地址和域名映射，只替换四个镜像引用；
4. 重建 Hardware、Egress、Control，通过健康检查后逐条重建 Engine；
5. 将实际安装的镜像 ID 和归档 SHA-256 写入 `update/installed-images.json`；
6. 任一切换或健康检查失败时自动恢复旧 Compose、三个基础容器和原有 Engine 镜像。

助手从旧 Control 镜像启动，Control 自身被替换后仍能继续运行。它只挂载项目数据目录和
Docker socket，不使用宿主 PID、网络或特权模式。更新期间不要关闭 NAS；完成后 WebUI 会要求
重新登录。

无法使用 WebUI 时可按相同边界手工恢复：

1. 下载目标 Release 的新 Compose YAML 和本机架构镜像资产，核对 `SHA256SUMS`；
2. 备份数据目录并保留当前 Compose 文件和镜像；
3. 让 Container Manager 使用新 YAML 重新构建项目；
4. 检查 Hardware、Egress、Control，再等待每条 Engine 恢复；
5. 核对 NAS 默认路由、设备列表、国家出口、SWu 和 IMS；
6. 任一基础服务失败时恢复旧 Compose 文件和旧镜像，并恢复更新前备份。

不要使用 Watchtower 或其他工具单独更新某一个组件；四种镜像必须保持同一版本。

## 8. 停止与卸载

在 Container Manager 中停止项目不会删除数据。删除项目前先确认是否保留数据目录：

- 保留数据：删除项目和项目容器，保留数据目录，之后可用同版本 Compose 恢复；
- 完全删除：先导出所需备份，再删除项目、MDD 命名卷和数据目录；
- 手工安装的内核驱动独立于 Compose。卸载应用项目不会移除它们，需要单独卸载。

不要手工删除仍被其他容器使用的 Docker 网络、卷或镜像。MDD 只管理带
`io.mdd-sim-gateway.managed=true` 标签的资源。

## 9. 常见问题

### Hardware 不健康

先确认 USB 设备已被宿主枚举，再按第 2 节的命令检查所需设备节点是否齐全；节点缺失即表示
宿主缺少对应内核驱动。`docker logs mdd-sim-gateway-hardware` 会给出主管进程的失败原因。
系统升级后必须按完整 build 和内核重新匹配驱动，不能强制加载旧驱动。

### 读卡器没有出现

检查是否有宿主 pcscd 或其他容器占用了设备。Hardware 容器内已经包含 pcsc-lite、libccid
及项目验证的读卡器补丁，宿主不应同时启动 pcscd。

### 国家出口订阅在 NAS 浏览器能打开，Egress 却无法访问

如果订阅 URL 使用 NAS 自己的公网域名，配置 `MDD_NAS_HOSTNAME` 为该域名，使其只在 Egress
容器内指向宿主网关。不要把订阅 URL 改成容器 IP，也不要修改 NAS 全局 DNS。

### 4G 开启后担心影响 NAS 网络

检查 NAS 默认路由仍指向原有 LAN 网关。蜂窝 profile 使用 `never-default`，只允许 Hardware
管理蜂窝接口。如果诊断发现其他接口被 NetworkManager 接管，系统会关闭蜂窝配置。

### 页面可以打开，但 VoWiFi 无法注册

依次检查读卡器/SIM 通道、国家出口 UDP、ePDG 解析、SWu 和 IMS 状态。软件支持路径不代表
运营商一定允许该 SIM、套餐、地区和设备身份使用 Wi-Fi Calling。

提交新的 NAS 兼容信息时，请使用仓库的 **NAS hardware compatibility** Issue 模板，并先
阅读[目录隐私与驱动准入规则](../drivers/README.md)。
