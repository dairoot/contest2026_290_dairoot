# RK3576 SDK 构建与打包执行参考

对应本项目 KickPi K7 的 Linux + openvela AMP 配置，实测日期 2026-09-15，SDK 位于 `~/rk3576-sdk/rk3576-linux`。下面的路径、数值和报错都来自该次实际执行；换机器或换 SDK 版本先核对再照用。SDK 不随本仓提供，从 KickPi 官方公开分享的 `rk3576_data` 获取：
https://pan.baidu.com/s/1ZXgbmzjDFurXs8w8NeSDUw?pwd=z34d ，提取码 `z34d`。链接内容未在本次会话核验，下载后按下面《与仓库配置对账》一节确认版本与叠加状态。

约定：`SDK` 为 SDK 根目录，`CONTEST_ROOT` 为本仓 checkout，`WS` 为 openvela 工作区（`WS/nuttx/nuttx.bin` 是从核固件）。

## 目标一览

`./build.sh` 的目标由 `device/rockchip/common/build-hooks/*.sh` 注册，完整列表见 `output/.make_usage`。常用：

| 目标 | 作用 | 产物 |
| --- | --- | --- |
| `<defconfig>` | 切换板型配置，重写 `output/.config` | `output/defconfig` 软链 |
| `print-parts` | 打印分区表 | 仅输出 |
| `kernel` | 编内核与 dtb | `kernel-6.1/boot.img` |
| `uboot` / `loader` | 编 U-Boot 与 SPL loader | `u-boot/uboot.img`、`u-boot/rk3576_spl_loader_*.bin` |
| `misc` | 打 misc | `output/misc.img` |
| `rootfs` | 按 `RK_ROOTFS_SYSTEM` 构建根文件系统 | `output/rootfs/` |
| `amp` | 编并打从核固件，**会覆盖 `output/firmware/amp.img`** | 同左 |
| `firmware` | 重新登记镜像 + 尺寸校验，末尾自动跑 `updateimg` | `output/firmware/` |
| `updateimg` | 只装箱，不编译 | `output/update/Image/update.img` |
| `all` | 开头 `rm -rf output/firmware`，再全量构建 | 全部 |

`rockdev/` 是 `output/firmware` 的软链（`mk-firmware.sh` 每次重建）。`output/sessions/<时间戳>/` 保留每次执行的 `.config`、`initial.env`、`final.env` 和分目标日志，`output/sessions/latest` 指向最近一次——排查"上次是怎么打出来的"先看这里。

## 本项目的分区表

`RK_PARAMETER="parameter-amp.txt"`，`./build.sh print-parts` 实测：

```
uboot    0x00004000  4M
misc     0x00006000  4M
boot     0x00008000  64M
recovery 0x00028000  128M
backup   0x00068000  32M
amp      0x00078000  2M
rootfs   0x00079000  grow
```

`amp` 是本项目在 `parameter-amp.txt` 里新增的分区，原厂分区表没有。`recovery` 在表里但本项目不产 `recovery.img`，打包时被静默跳过。

## 宿主机依赖与实测报错

`check-sdk.sh` 对 `python3`、`rsync`、`gcc`、`g++` 做 `which` 检查，任一缺失即退出：

```
Your gcc is missing
Please install it:
sudo apt-get install gcc
ERROR: Running ./build.sh - check_sdk failed!
```

`mk-firmware.sh` 另需 `fakeroot`（本机未装，所以 `firmware`、`all` 两个目标当前跑不通，`updateimg` 不受影响）。

本机没有系统 `gcc/g++/make`，conda 只有 `gcc` 和 `make`、没有 `g++`/`cc1plus`。`updateimg` 不编译任何东西，做临时 shim 通过检查即可：

```bash
SHIM=$(mktemp -d)
ln -sf ~/miniconda3/bin/gcc  "$SHIM/gcc"
ln -sf ~/miniconda3/bin/gcc  "$SHIM/g++"     # 仅用于通过 which 检查
ln -sf ~/miniconda3/bin/make "$SHIM/make"
export PATH="$SHIM:$PATH"
```

真要编内核、u-boot 或 buildroot 时这个 shim 不够用，必须装可用的 C/C++ 工具链。

## 把 openvela 打进 amp.img

`./build.sh amp` 走的是 SDK 自带的从核方案。本机 SDK 当前处于 RT-Thread 变体：`device/rockchip/.chips/rk3576/amp-k7.its` 带 `compile { sys = "rtt"; }` 节点、板级 defconfig 里 `RK_AMP_RTT_TARGET="rk3576-k7-amp"`，与 `CONTEST_ROOT/board/contest_board/linux-side/configs/` 下的 openvela 版本不一致（后者把两个 `RK_AMP_*_TARGET` 置空、`.its` 无 `compile{}`）。**照跑 `./build.sh amp` 得到的是 RT-Thread 固件，不是 openvela**，且会直接覆盖 `output/firmware/amp.img`。

手工打包（与 `tools/kws/deploy/flash_amp.sh` 同一套做法，不依赖 `mk-amp.sh`，也不需要它那两个前置）：

```bash
OUT=$(mktemp -d)
cp "$WS/nuttx/nuttx.bin" "$OUT/rtt3.bin"                      # .its 里 incbin 的文件名
sed '/share {/,/}/d;/compile {/,/}/d' \
    "$CONTEST_ROOT/board/contest_board/linux-side/configs/amp-k7.its" > "$OUT/amp.its"
cd "$OUT"
export PATH="$SDK/kernel/scripts/dtc:$PATH"                    # mkimage 要调 dtc
"$SDK/rkbin/tools/mkimage" -f amp.its -E -p 0xe00 amp.img

cp "$SDK/output/firmware/amp.img" "$OUT/amp.img.prev.bak"      # 先备份再覆盖
cp amp.img "$SDK/output/firmware/amp.img"
```

`sed` 删的是 rockchip FIT 源里 `mk-amp.sh` 本该剥掉的构建期节点，交给裸 `mkimage` 前必须自己删。

2026-09-15 实测：`nuttx.bin` 1609728 B → `amp.img` 1614336 B，占 789/4096 扇区，2MB 分区放得下；被覆盖的那份是 8/12 的 RT-Thread 镜像 1421824 B。`mkimage` 会回显 FIT 描述、load 地址 `0x41800000`、cpu `0x3` 和 sha256，核对这几项比核对文件大小可靠。

`amp` 分区只有 2MB，从核固件的 `.bss` 里放大数组会被 `objcopy` 填零撑爆——历史上出过这个问题，改成堆分配解决。手工覆盖不触发 `mk-firmware.sh` 的尺寸校验，自己比。

## 打 update.img

```bash
cd "$SDK"
export PATH="$SHIM:$PATH"
./build.sh updateimg
```

流程：从 `output/firmware/` 软链取件 → 生成 `package-file` → `afptool -pack` 出 `update.raw.img` → `rkImageMaker` 套 loader 头、算 MD5 出 `update.img`。工具在 `tools/linux/Linux_Pack_Firmware/rockdev/`。

开头会 `rm -rf output/update output/firmware/update.img`，**上一份整包不保留**。raw 与成品并存，磁盘按两倍整包预留。

2026-09-15 实测输出（约 1 分钟）：

```
output/update/Image/update.img   4151609987 B (3.9G)
output/firmware/update.img                        -> 软链
output/firmware/update-rk3576-kickpi-k7-linux-amp-buildroot-<时间戳>.img -> 软链
```

打印出的 package-file 就是实际装箱清单，逐行核对：

```
package-file parameter bootloader uboot misc boot backup(RESERVED) amp rootfs
```

`oem.img`、`userdata.img` 在 `output/firmware/` 里但不在分区表里，没有进包也没有提示。

成品头四字节应为 `RKFW`：

```bash
head -c 4 output/update/Image/update.img | od -c | head -1
```

## 本项目各镜像的实际来源（2026-09-15 快照）

| 镜像 | 解析后的真实路径 | 时间 |
| --- | --- | --- |
| `MiniLoaderAll.bin` | `u-boot/rk3576_spl_loader_v1.09.108.bin` | 8/11 |
| `uboot.img` | `u-boot/uboot.img` | 8/11 |
| `misc.img` | `output/misc.img` | 8/11 |
| `boot.img` | `kernel-6.1/boot.img` | 8/12 |
| `amp.img` | 手工打包的 openvela FIT | 9/15 |
| `rootfs.img` | `~/rk3576-sdk/rootfs/ubuntu-rootfs-20250909.img` | 2025-09-09 |

`RK_ROOTFS_SYSTEM="buildroot"`，但 `rootfs.img` 是手工软链过去的预制 Ubuntu 镜像，SDK 没有参与构建它。整包里因此**不含** `linux-apps/` 下的服务、`snd_rpmsg_mic.ko`、`kws-micpower.service` 等 systemd 单元，以及 `linux-side/rootfs/etc/NetworkManager/conf.d/wifi-powersave-off.conf`；刷整包会把板上这些手工装的东西一起抹掉，需按部署脚本重装。

`boot.img` 对应的两个 dts（`rk3576-kickpi-k7-amp.dtsi`、`rk3576-kickpi-k7-linux-amp.dts`）与 SDK 里的版本逐行比对只差注释；内核 config 片段仓库叫 `amp-rpmsg.config`、SDK 叫 `amp-rtt.config`，同样只差首行注释，功能等价。也就是说 SDK 与仓库的 RT-Thread/openvela 分歧只实质影响 `amp.img`。

## 与仓库配置对账

```bash
cd "$CONTEST_ROOT/board/contest_board/linux-side"
diff dts/rk3576-kickpi-k7-amp.dtsi        "$SDK/kernel-6.1/arch/arm64/boot/dts/rockchip/"
diff dts/rk3576-kickpi-k7-linux-amp.dts   "$SDK/kernel-6.1/arch/arm64/boot/dts/rockchip/"
diff configs/parameter-amp.txt            "$SDK/device/rockchip/.chips/rk3576/"
diff configs/amp-k7.its                   "$SDK/device/rockchip/.chips/rk3576/"
diff configs/rockchip_rk3576_kickpi_k7_buildroot_amp_defconfig \
                                          "$SDK/device/rockchip/.chips/rk3576/"
```

只差注释可以照用；`.its` 多出 `compile{}`、defconfig 里 `RK_AMP_RTT_TARGET` 非空、`RK_KERNEL_CFG_FRAGMENTS` 指到别的文件，都说明 SDK 停在另一条链路上。首次叠加改动的完整步骤见 `board/contest_board/README.md` §三.2。

## 烧写

整包（升级模式，板子进 loader/maskrom）：

```bash
"$SDK/tools/linux/Linux_Upgrade_Tool/Linux_Upgrade_Tool/upgrade_tool" uf \
    "$SDK/output/update/Image/update.img"
# 等价：cd "$SDK" && ./rkflash.sh update
```

Windows 侧用 RKDevTool 升级模式装同一个文件。`rkflash.sh all` 是逐分区刷，会去找本项目没有的 `trust.img`、`recovery.img`，不要用。

只迭代从核固件（板子已刷过 AMP 整包、系统能起来时最快，约 10 秒）：

```bash
scp amp.img <板子>:/tmp/
ssh <板子> 'sudo dd if=/dev/disk/by-partlabel/amp of=/tmp/amp_backup.img bs=1M count=8 status=none
            sudo dd if=/tmp/amp.img of=/dev/disk/by-partlabel/amp conv=fsync status=none
            sudo reboot'
```

仓库里 `tools/kws/deploy/flash_amp.sh` 把"构建 → 打 FIT → scp → 备份 → dd → 重启"串成一条；首次使用先核对里面的板子地址、SDK 路径和分区名。

## 上板验收

```bash
cat /proc/device-tree/model            # 应为 AMP 变体
lsblk -o NAME,PARTLABEL,SIZE           # 分区表与刚烧的一致，有 amp 分区
sudo busybox devmem 0x47c00004         # 从核心跳版本号，openvela 为 0x3
```

再按 `board/contest_board/README.md` §三.4 验心跳计数、RPMsg 双向通信和 UART5 上的 NuttShell。只确认能开机不算验收。
