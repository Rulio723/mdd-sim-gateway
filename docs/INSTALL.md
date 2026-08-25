# 安装与升级

## 支持环境

- 推荐 ARM64 Debian、Ubuntu 或 Armbian，systemd 可用。
- Docker、USB、内核 TUN、pcscd；蜂窝模块还需要 ModemManager/NetworkManager。
- 已实机验证的三体电子 SCR Prime（`04d9:c001`）提供标准 CCID 接口，但尚未进入 libccid 1.6.2 的设备表。连接该型号时执行 `sudo ./install.sh patchprime`，安装程序会从校验过的固定版本源码构建驱动并加入设备匹配；完成后支持热插拔。
- 至少 4 GB 可用磁盘。ARM64 正式版本升级会通过与源码包相同的直连或代理通道下载 CI 在
  原生 ARM64 runner 构建、并按校验和、版本与源码指纹核验的 Engine；设备不再为了 Engine
  变更编译 Asterisk。同一镜像仍发布到 GHCR，供手工安装与独立核验。amd64 没有对应的
  Release 镜像资产，升级器会保留架构边界并在本机刷新或构建原生 Engine，绝不导入 ARM64
  镜像。
- 手工执行全新 Engine 构建时，固定 commit 从项目维护的 GitHub sysmocom 镜像获取；
  镜像只保存构建所需的上游分支，原始项目与许可归属不变。离线迁移仍可使用已经审核的
  `MDD_ENGINE_BASE_IMAGE`，不得关闭 TLS 验证或改用未审核源码。

## 安装

```bash
sudo ./install.sh install                 # 原生控制面 + Docker 引擎
sudo ./install.sh install --mode docker   # 控制面也运行在 Docker
```

可用环境变量：`MDD_PORT`、`MDD_DATA_DIR`、`MDD_BIND`、`MDD_ADVERTISE_ADDR`、`MDD_SINGBOX_VERSION`、`MDD_XRAY_VERSION`、`MDD_LPAC_VERSION`。安装程序会校验 sing-box 与 Xray-core 归档的 SHA-256；Xray-core 仅用于 Reality/XHTTP 节点的本机回环兼容层。更换固定依赖版本时必须同步审核并更新 SHA-256。离线迁移时可显式设置 `MDD_ENGINE_BASE_IMAGE`，从本机已审核的兼容引擎镜像创建只覆盖 MDD 运行脚本与模板的镜像；已经在可信构建机完成 `npm ci && npm run build` 时，也可设置 `MDD_REUSE_WEBUI=1` 复用随源码传入的 `webui/dist`。全新在线安装不要设置这两项，仍执行完整源码构建。必须执行全量 Engine 构建、但安装网络无法访问默认 GitHub mirror 时，可将 `PJPROJECT_REPOSITORY` 和 `ASTERISK_REPOSITORY` 显式指向另一条经过审核且包含相同固定 commit 的 HTTPS Git 仓库；未设置时继续使用 Dockerfile 中的项目 mirror。不得关闭 TLS 验证或改用未经审核的源码。

`MDD_DATA_DIR` 在首次安装后会写入系统状态；后续执行 `status`、`reload` 和 `uninstall` 时不必再次填写，避免自定义数据目录被误判为新安装。

如果系统 Docker 已经可以连接，安装脚本只复用它，不升级版本、不修改 daemon 配置、不执行 prune，也不操作其他项目的容器或镜像。MDD 容器带有归属标签；发现同名外部容器、8443 端口冲突或 rootless Docker 时会停止并给出错误。蜂窝与 TUN/PCSC 引擎需要系统级 Docker daemon，因此不支持 rootless 模式。

版本检查始终使用 GitHub Release API，不读取或发送 GitHub Token。配置的仓库不可访问或尚未发布 Release 时，界面会显示尚无可用发布版本。

安装完成后，在受信的局域网或 VPN 中立即打开 `https://主机地址:8443`，创建至少 10 字符的管理员密码。首次设置完成前，任何能访问该端口的客户端都可申领初始管理员。配置自有证书时，证书和私钥应只允许 root 读取。运行数据目录默认为 `0700`，凭据文件为 `0600`。

## 更新

系统设置可在“自动更新”和“提示更新”中二选一，并分别选择全部版本或主版本。主版本不按版本号推断，而由 `update-policy.json` 的 `release.kind` 明确标记为 `main`；其他版本标记为 `patch`。新安装默认自动更新主版本；自动模式不再发送发现新版本提示，匹配的版本仍必须由同一策略文件单独标记为稳定、匹配完全相同的版本并到达 `not_before` 时间，单纯发布 Release 不会触发安装。提示模式默认提示全部版本，左下角版本号出现红点后，由管理员查看说明并确认“立即升级”。更新时控制面把请求写入编排器目录，主机上的 `mdd-sim-gateway-orchestrator` 以独立的临时 systemd 单元（`mdd-sim-gateway-update`）运行 `host/mdd_update.py` —— 下载对应 `vX.Y.Z` Release 资产、校验 SHA-256 和版本，并比较新源码与本机 Engine 指纹。Engine 输入发生变化时，更新器通过同一条直连或代理回退线路下载该版本的 ARM64 Engine 资产，校验后导入 Docker，再核对架构、版本和两类指纹；输入未变化时不会重复下载。备份与覆盖源码后，安装器保存旧 Engine 的 `:previous` 回滚标签，启用新镜像并只重建旧镜像上的线路，控制面重新扫描在位 SIM 使线路自愈。Docker 控制面模式还会取得已校验的 ARM64 控制镜像资产并执行 `docker load`。`data/`、`.env`、`.git` 和虚拟环境均保留。日志见 `journalctl -u mdd-sim-gateway-update`、数据目录下 `update/reload.log` 与 `update/engine-image.log`。

“系统设置 → 备份与更新”默认使用“自动”联网：先直连 GitHub，连接失败、超时或被限流时，再按代理库顺序尝试可用条目；检查成功的线路会继续用于更新下载。也可选择“仅直连”或指定一个代理库条目。SOCKS5 条目可直接使用；订阅、具体节点和导入的 outbound 需已分配给一个已启用且就绪的国家出口。代理凭据只保存一份，并只通过主机权限为 `0600` 的配置/临时文件传递，不写入 systemd 命令行或升级状态。
控制面不依赖浏览器登录，每 6 小时检查一次 Release。提示更新模式会通过已启用的 Webhook、Telegram 或 PushPlus 通道发送一次去重通知；选择“仅主版本”时，只处理在 `update-policy.json` 中明确标记为 `main` 的 Release，不从版本号位数推断。
正式 Release 归档包内含 CI 预构建的 `webui/dist`，一键升级校验整个归档后直接复用，因此不需要在树莓派上下载 Node 镜像或编译前端。GitHub `main` 与其 Release 是唯一支持的更新通道。

`v1.4.1` 的升级器早于 Engine Release 资产，完成源码校验后会调用新版本安装器并要求保留旧
Engine。为允许用户直接跨级，正式源码包额外携带一次性 Engine 校验清单；在 ARM64 上，新
安装器会读取旧升级任务尚未删除的私有线路文件，以相同的直连和代理候选下载、校验并导入
Engine；在 amd64 上则撤销旧升级器的 Engine 保留请求，转入本机原生刷新或构建。成功后清单
即被删除，不改变日常手工执行 `--no-engines` 的含义，也不要求先安装桥接版本。

也可以随时在主机上手动更新：备份并用受信任来源更新源码后执行：

```bash
sudo ./install.sh reload --engines
```

该方式保留数据并从固定源码重建依赖与引擎；正式一键升级优先使用 CI 分发镜像。

正式发布前请逐项完成 [发布检查清单](RELEASE_CHECKLIST.md)。推送与 `VERSION` 一致的 `vX.Y.Z` 标签后，Release 工作流会运行全套测试，并生成带 SHA-256 校验文件的源码包。

## 卸载

`sudo ./install.sh uninstall` 保留数据；`--purge` 会删除运行数据与虚拟环境，无法恢复。卸载只移除确认属于 MDD 的容器；Docker 本身及其他项目不受影响。
