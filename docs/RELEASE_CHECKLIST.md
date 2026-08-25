# 发布检查清单

## 代码与版本

- `VERSION`、WebUI `package.json` 与标签保持一致（例如 `1.0.0` / `v1.0.0`）。
- `CHANGELOG.md` 将目标版本从 `Unreleased` 改为发布日期。
- CI 的 Python 测试、WebUI 构建、生产依赖审计和脚本语法检查全部通过。
- Release 必须包含 `mdd-sim-gateway-control-vX.Y.Z-arm64.tar.gz` 和
  `mdd-sim-gateway-engine-vX.Y.Z-arm64.tar.gz`，且 `SHA256SUMS` 同时覆盖源码包和两个
  镜像。发布前用 `docker load` 验证资产为 `linux/arm64`、版本 label 与 `VERSION`
  一致；不得只发源码包。
- Release 工作流必须在原生 `ubuntu-24.04-arm` runner 无缓存构建 Engine，通过模块数、
  Python 依赖和 Asterisk 版本检查，并推送
  `ghcr.io/mddidd/mdd-sim-gateway-engine:vX.Y.Z`；package job 必须等待该 job 成功。
- 依赖版本、源码提交与二进制 SHA-256 已复核；不得临时改成浮动分支或 `latest`。

## ARM64 实机验收

- 在目标 ARM64 设备通过一键更新下载本次 Engine Release 资产，确认直连失败或过慢时能
  切换到代理，核对架构、版本和源码指纹，并完成一次真实 SIM 线路重建与注册；编译由原生
  ARM64 CI 完成，设备本身不得重新编译镜像。另行抽查同版本 GHCR 镜像身份一致。
- 必须另从仍运行 `v1.4.1` 旧升级器的设备直接升级到本版本，确认源码包内的一次性 Engine
  接力清单生效：不要求先安装桥接版本，不直连 GHCR，也不能因旧升级器的 `--no-engines`
  参数留下旧镜像。
- 全新安装与重复安装均成功，断电重启后管理面自动启动。
- ModemManager/NetworkManager、pcscd、sing-box、lpac 状态符合预期。
- 已有 Docker 与外部容器保持不变；MDD 容器均带归属标签，端口冲突会安全中止。
- 至少验证一个蜂窝模块的 4G 开/关、VoWiFi 开/关、通话与短信。
- 至少验证一个 PC/SC 读卡器仅显示 VoWiFi，不显示虚假的 4G 能力。
- 多模块时逐台切换能力，确认不会改动另一台模块。
- Clash 订阅国家出口通过 UDP 测试，界面显示实际节点名称；无健康节点时故障关闭。
- Webhook GET/POST、Telegram 代理与 PushPlus 测试按钮均验证一次。
- Telegram 仅发送通知，设置页和后端均不存在远程拨号、短信或挂断指令入口；
  直接回复来信通知能给该号码回短信；相关操作出现在审计记录中。
- 自有 TLS 证书、首次管理员设置、修改密码、备份和脱敏支持包均验证一次。

## 隐私与发布

- 订阅者标识符（IMSI、ICCID、IMEI、号码）由 `tools/check-subscriber-identifiers.sh` 自动扫描，
  CI 与发布流程均已强制执行，无需手工核对。
- 仍需人工检查脚本覆盖不到的部分：EID、PIN、Token、订阅地址、私钥，以及截图内容。
- `data/`、`.env`、证书、pcap、数据库、构建目录和本机日志未被 Git 跟踪。
- 截图仅使用空状态、虚构数据，或已经逐项遮挡设备、线路、运营商、国家出口、号码与消息内容并经人工复核的真实页面。
- 先创建私有仓库完成内部验收；最终确认后再决定是否公开。
- 推送已签名的 `vX.Y.Z` 标签；Release 工作流会生成源码包、ARM64 控制镜像、ARM64 Engine
  镜像及同时覆盖三者的 `SHA256SUMS`，并把同一 Engine 发布到 GHCR 和 Release 资产。
- **Release 说明改写为简短的中英双语，中文在前、英文在后，两者内容一致。**
  工作流用 `--generate-notes` 只生成提交列表，那是给写代码的人看的，不是给升级的人看的；
  发布后必须用 `gh release edit vX.Y.Z --notes-file <文件>` 替换。每条按
  「症状 → 原因 → 现在的行为」写一到两句：使用者据此判断该不该升级，而提交标题做不到这件事。
  若本版未重建引擎镜像，在结尾注明，免得对方做多余的构建。
- 发布 Release 后，在 `update-policy.json` 的 `release` 中填写该版本，并按发布内容明确标记为 `main`（包含功能变化）或 `patch`（仅修复）；这个分类只控制“仅主版本”的筛选，不代表允许自动安装。先完成观察和实机验证，只有决定向已主动开启自动更新的设备推送时，才在 `auto_update` 中另行填写同一目标版本和 UTC `not_before`；二者必须与当前最新正式 Release 完全一致。发现回归时清空自动更新许可即可阻止尚未开始的设备安装。
- ARM64 交叉构建时，WebUI 阶段必须保持在 Docker `BUILDPLATFORM` 原生架构；前端产物是
  与架构无关的静态文件，不得在 GitHub x86 Runner 的 ARM64 QEMU 中执行 `npm ci`。
