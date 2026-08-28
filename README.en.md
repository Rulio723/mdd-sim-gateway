<p align="center">
  <img src="assets/logo-lockup.svg" width="520" alt="MDD Sim Gateway">
</p>

<p align="center"><strong>Turn physical SIMs and eSIMs into a self-hosted gateway for VoWiFi, calls, SMS and isolated network egress.</strong></p>

<p align="center">
  <a href="README.md">中文</a> ·
  <a href="#quick-install">Quick install</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/INSTALL.md">Installation</a> ·
  <a href="https://github.com/MddIdd/mdd-sim-gateway/discussions">Discussions</a>
</p>

MDD Sim Gateway is a self-hosted multi-SIM communications gateway for Debian, Ubuntu and Armbian ARM64 hosts. It brings cellular modems, USB smart-card readers, IMS, EAP-AKA, eSIM, ModemManager and sing-box into one bilingual Web console.

| Real SIM authentication | Calls and SMS | Multi-modem control | Isolated country exits |
|---|---|---|---|
| Perform EAP-AKA and IMS-AKA inside a physical SIM/eSIM without reading Ki/OP/OPc | Browser softphone, SMS, call history and incoming notifications | Manage cellular modems, PC/SC readers and eUICCs in one console | Route each SIM's ePDG through its own country TUN and fail closed when UDP checks fail |

## Interface tour

![MDD Sim Gateway English interface tour (fictional demo data)](assets/product-tour.gif)

<p align="center">Overview → device management → browser calling → messages → balance & keeping → system updates · All identities and content shown are fictional demo data</p>

## Quick install

Use an ARM64 Debian, Ubuntu or Armbian host with systemd, Docker, USB and a stable network connection.

Storage requirements: keep at least **4 GiB free on the root filesystem** before installation.
A **16 GB or larger** system disk is recommended, with about **6 GiB free** before an upgrade so
the new image and one rollback generation can coexist. Development checkouts and explicit source
builds create substantially more temporary build cache and are unsuitable for space-constrained
devices. Enlarging a VM's virtual disk alone is not enough: grow its root partition and filesystem,
then use `df -h /` as the authoritative capacity.

```bash
git clone https://github.com/MddIdd/mdd-sim-gateway.git
cd mdd-sim-gateway
sudo ./install.sh install
```

When installation completes, open `https://<gateway-address>:8443` and create the administrator account immediately on a trusted LAN or VPN. See [Installation](docs/INSTALL.md) for prerequisites, the full install process and upgrades.

> This software directly controls cellular radios, SIMs, network routes and IMS. Carrier support for Wi-Fi Calling still depends on the plan, region, device identity and network policy.

## Architecture

![MDD Sim Gateway architecture](docs/architecture.svg)

## Full screenshots

<details>
<summary>View the Overview, Devices, Calls, Messages, Balance & keeping, and System updates screens</summary>

![MDD Sim Gateway English overview (fictional demo data)](screenshots/overview-redacted.en.png)

![MDD Sim Gateway English devices page (fictional demo data)](screenshots/devices-redacted.en.png)

![MDD Sim Gateway English calls page (fictional demo data)](screenshots/calls-redacted.en.png)

![MDD Sim Gateway English messages page (fictional demo data)](screenshots/messages-redacted.en.png)

![MDD Sim Gateway English balance and number keeping page (fictional demo data)](screenshots/keepalive-redacted.en.png)

![MDD Sim Gateway English system updates page (fictional demo data)](screenshots/settings-redacted.en.png)

</details>

## Capabilities

- Detect supported ModemManager cellular modules and ordinary PC/SC readers automatically.
- Control 4G data, radio flight mode and VoWiFi independently for each physical modem.
- Show balance, plan expiry, network presence and keeping results on one page. Prepaid lines can
  schedule a real chargeable SMS, while plan lines can watch the renewal balance and warn when low.
- Perform EAP-AKA and IMS-AKA in the physical SIM/eSIM without reading or storing Ki/OP/OPc.
- Show each modem UICC's three logical-channel allocations, roles and explicit failures.
- Provide an authenticated browser softphone, SMS, call history, missed-call notifications and
  per-line local voicemail. Recordings remain on the gateway and are never attached to notifications
  or support bundles; standalone SIP clients are not accepted.
- Maintain reusable subscriptions, individual nodes and SOCKS5 proxies, then assign one to each
  country. sing-box owns the isolated TUNs; Xray-core carries Reality/XHTTP nodes. VoWiFi fails
  closed unless the selected exit passes a runtime UDP check.
- Send standard/custom Webhooks, Telegram notifications and PushPlus messages.
- Check releases every six hours in the background. Choose automatic installation or notify-only,
  scoped to main releases or every release. Unattended installation still requires the exact version
  and earliest rollout time to be approved separately in `update-policy.json`. All-version
  devices follow the approved latest Release, while main-only devices follow an independently
  configured main Release even after newer patches have been published.
- Telegram is notification-only and does not accept remote control commands.
- Manage eUICC profiles through a pinned local lpac build, including dual-SE readers.
- Offer HTTPS, first-run administrator setup, persistent 12-hour or 30-day sessions, CSRF protection,
  login throttling, local backups, audit records, redacted support bundles and release checks.

| Hardware | 4G data | Wi-Fi Calling | SIM access |
|---|---:|---:|---|
| ModemManager-compatible cellular module | Yes | Yes | Modem APDU/logical-channel bridge |
| DJI/Quectel EC25-class module | Yes | Yes | Automatically provisioned virtual slots |
| USB PC/SC reader | No | Yes | Direct PC/SC |
| Santi Electronics SCR Prime (`04d9:c001`) | No | Yes | Direct PC/SC; install with the `patchprime` driver patch |
| eUICC/eSIM reader | No | Yes | PC/SC and lpac |

The Santi Electronics SCR Prime has been verified on physical hardware. Support in this table
describes the implemented path; it does not guarantee that every SIM, firmware build or carrier
will permit the service.

## What the installer does

The installer reuses a working system Docker daemon, or installs the distribution package when
Docker is absent. It provisions pcscd, ModemManager/NetworkManager, checksummed sing-box and
Xray-core, a pinned
lpac source build, the Web console and the per-SIM VoWiFi engine. It does not prune Docker or
modify unrelated containers.

Common commands:

```bash
sudo ./install.sh status
sudo ./install.sh logs
sudo ./install.sh reload
sudo ./install.sh build-lpac
sudo ./install.sh uninstall
```

See [installation](docs/INSTALL.md), [architecture](docs/ARCHITECTURE.md),
[troubleshooting](docs/TROUBLESHOOTING.md) and [security](SECURITY.md) for details.

## Responsible use

> **Compliance warning:** This software is only for use by the verified subscriber of a number where the carrier expressly permits that use. Do not use it for fraud, bulk or nuisance calling, marketing, verification-code collection, renting numbers or lines, call forwarding for others, concealing the controller's location, or providing telecommunications services to third parties. Users must follow local law, subscriber identity rules, and carrier terms. This project grants no telecom licence or carrier authorisation. MDD Sim Gateway stores and runs at most **five SIM lines** and provides neither standalone SIP accounts nor Telegram commands for calls, SMS, or hangup. Technical restrictions do not make any particular use lawful.

## Community and feedback

- Installation, hardware and carrier compatibility: [GitHub Discussions](https://github.com/MddIdd/mdd-sim-gateway/discussions)
- Reproducible defects and concrete feature requests: [GitHub Issues](https://github.com/MddIdd/mdd-sim-gateway/issues/new/choose)
- Code and documentation contributions: [CONTRIBUTING.md](CONTRIBUTING.md)

If the project is useful to you, save it on GitHub and share a redacted hardware or carrier compatibility result.

## Country exits

Add one or more subscriptions, individual nodes or SOCKS5 servers to the proxy library, then assign
one to each country. Subscription exits retain name filtering and automatic/manual node selection;
individual nodes and SOCKS5 entries are used directly. Reality/XHTTP share links use a loopback-only
Xray-core bridge. The eye control is off by default, masking subscription URLs, node links and
SOCKS5 details. A separate UDP probe is mandatory because IKEv2/ESP NAT traversal depends on UDP
500/4500. Only that SIM's ePDG routes enter the country's dedicated TUN.

## Security and privacy

- Administrator passwords use salted scrypt hashes. Session cookies are HttpOnly, Secure and
  SameSite=Strict; state-changing requests require a CSRF token.
- Engine callbacks use a random per-install token.
- Runtime data directories are owner-only and credential-bearing files are written as mode 0600.
- Support bundles redact identities, URLs, notification credentials, activation codes and
  cryptographic material. Review every bundle before sharing it.
- The product has no analytics or telemetry. Network requests occur only for configured
  carrier/IMS operation, subscriptions, notifications, eSIM provisioning, dependency installation
  and periodic release/promotion checks.
- Do not expose Docker, ModemManager, pcscd, SIP, AMI or the management port directly to the
  public Internet. Prefer a trusted LAN or VPN and a trusted TLS certificate.

## License and acknowledgements

MDD Sim Gateway is released under **GPL-3.0-only**. Build-time derivative patches that must remain
under an upstream license are identified separately. The project is a derivative of
[pagecat/vowifi_gateway](https://github.com/pagecat/vowifi_gateway) (MIT), which contributes the
VoWiFi engine and the overall control-plane/engine/WebUI architecture; MDD Sim Gateway adds 4G
cellular data and SMS, per-country network egress routing, unified device management and automatic
provisioning, failover and a test suite. It further derives from or interoperates with SWu-IKEv2,
sysmocom Asterisk and pjproject, phcoder/asterisk-docker, mitshell/card, sing-box, lpac, PCSC,
CCID, pyscard and frankmorgner/vsmartcard. See [NOTICE](NOTICE) and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

This is an independent project and is not endorsed by carriers, hardware vendors or upstream
projects.
