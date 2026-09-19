# KickPi K7 的麦克风供电与 zram 服务

这两个单元用于本项目的 KickPi K7 / RK3576 Ubuntu AMP 系统：

| 单元 | 用途 |
| --- | --- |
| [kws-micpower.service](../../../tools/kws/deploy/kws-micpower.service) | 开机拉高 GPIO3_D0，为当前接在排针 pin 30 的麦克风供电 |
| [zram-swap.service](../../../board/contest_board/linux-side/rootfs/etc/systemd/system/zram-swap.service) | 直接使用内核内置的 zram0，配置 zstd、3 GiB、swap 优先级 100 |

文件来源：2026-09-19 从开发板的 `/etc/systemd/system/` 只读导出，两个服务均为
`enabled` / `active (exited)`，没有 drop-in；GPIO 读值为 `1`，zram 容量为
`3221225472` 字节、算法为 `[zstd]`、优先级为 `100`，发行版的
`zramswap.service` 为 `disabled` / `inactive`。入仓的麦克风单元与板上文件一致；
zram 单元仅将 shell 参数从 `-c` 改为 `-ec`，确保配置或 `swapoff` 失败时立即停止，
不继续执行格式化或 reset。这次只完成配置读取与静态检查，未替换板上服务、重启或烧写。

[已发布整包](../../../docs/firmware-2026-09-15.md)使用厂商预制 rootfs，**不包含
这两个单元**。仓库中的 `rootfs/etc/` 是待安装配置，不能据此认为它已进入 Release 镜像。

## 安装前核对

- 麦克风单元只适用于本项目的 **pin 30 / GPIO3_D0** 应急供电接法，先按
  [接线说明](../../../board/contest_board/docs/k7-40pin-pinout.md)确认。使用独立
  1.8V LDO 供电时不需要此单元，也不要同时连接 GPIO 电源。
- zram 单元要求 `/sys/block/zram0/` 已由内核提供、支持 `zstd`，且板上有
  `mkswap`、`swapon`、`swapoff`（Ubuntu 的 `util-linux`）。它不调用 `modprobe`。
- 不要让 `zramswap.service`、zram generator 或 `/etc/fstab` 同时管理 zram0。
  正在使用 swap 时不要直接重启服务或 reset 设备；先停掉推理负载，并确认可用内存
  足够容纳换出的页面。仅补交仓库文件时无需操作运行中的板子。

在**开发板**上检查：

```bash
cat /sys/class/leds/GPIO3_D0/brightness
cat /sys/block/zram0/comp_algorithm
cat /sys/block/zram0/disksize
swapon --show
systemctl status kws-micpower.service zram-swap.service zramswap.service --no-pager
```

## 复制并启用

在**开发板上的项目根目录**执行下列命令。若板上只有固件，先将仓库或这两个单元文件
复制到板上；不要在开发机上安装这些服务。已有自定义单元时先备份：

```bash
(
set -eu
SERVICE_BACKUP_DIR="/var/backups/contest-systemd-$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 0755 "$SERVICE_BACKUP_DIR"
for unit in kws-micpower.service zram-swap.service; do
    if sudo test -e "/etc/systemd/system/$unit"; then
        sudo cp -a "/etc/systemd/system/$unit" "$SERVICE_BACKUP_DIR/"
    fi
done
sudo install -m 0644 tools/kws/deploy/kws-micpower.service \
    /etc/systemd/system/kws-micpower.service
sudo install -m 0644 board/contest_board/linux-side/rootfs/etc/systemd/system/zram-swap.service \
    /etc/systemd/system/zram-swap.service
sudo systemd-analyze verify /etc/systemd/system/kws-micpower.service \
    /etc/systemd/system/zram-swap.service
sudo systemctl daemon-reload
sudo systemctl enable kws-micpower.service zram-swap.service
)
```

以上只安装并设置开机启动，不重启现有服务。若使用 LDO，跳过麦克风单元的安装与
enable，只安装 zram。若发行版的 `zramswap.service` 仍启用，先执行
`sudo systemctl disable zramswap.service`，并处理其他 zram0 管理配置；这里不加
`--now`，避免立即抽走当前 swap。确认业务可中断后，在计划内重启板子使配置生效。

**全新系统、zram0 尚未初始化且未被其他管理器占用时**，可以不重启，按需启动：

```bash
sudo systemctl start kws-micpower.service
sudo systemctl start zram-swap.service
```

独立供电时跳过第一条。已有 zram0 的系统应走前述重启流程，不要用这两条命令迁移
运行中的 swap。

## 检查与排障

```bash
systemctl is-enabled kws-micpower.service zram-swap.service
systemctl is-active kws-micpower.service zram-swap.service
cat /sys/class/leds/GPIO3_D0/brightness
cat /sys/block/zram0/comp_algorithm
cat /sys/block/zram0/disksize
swapon --show
journalctl -b -u kws-micpower.service -u zram-swap.service --no-pager
```

oneshot 服务显示 `active (exited)` 是正常状态。GPIO 应为 `1`；zram 应选中
`[zstd]`，容量为 `3221225472` 字节，`swapon` 显示 `/dev/zram0` 且优先级 `100`。
显示 `3G` 是配置容量，不代表增加了物理内存；实际占用随数据及压缩率变化。

GPIO 节点不存在时检查 Linux DTS 与接线，不要改用其他 GPIO 试错。zram 报
`Device or resource busy` 时检查是否已初始化、是否被其他服务占用；算法不支持时
检查内核配置。初始化失败后可能留下部分配置，处理原因后安排重启再试，不对正在
使用的 swap 直接执行 reset。zram 的 sysfs 配置与重置语义见
[Linux 内核文档](https://docs.kernel.org/admin-guide/blockdev/zram.html)。

服务状态正常后，仍需按 [KWS 验收步骤](project-workflow.md#验收与结果记录)
确认真实音频、唤醒事件，并检查 Linux ASR/声纹负载；静态 unit 校验不能替代真机验收。
