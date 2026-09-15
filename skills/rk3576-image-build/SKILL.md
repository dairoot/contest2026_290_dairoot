---
name: rk3576-image-build
description: 在 Rockchip RK3576 Linux SDK 上编译并打包可烧写镜像，覆盖 loader/uboot/boot/rootfs/AMP 各分区镜像的来源核对、分区表容量约束、update.img 整包打包、单分区迭代与烧写验收。用于出首刷整包、改完某一侧后重新打包、以及打包失败或刷完不对的排障；以 KickPi K7 的 Linux + openvela AMP 配置为已验证基线。
---

# 在 RK3576 Linux SDK 上编译与打包镜像

打包脚本只是把 `output/firmware/` 里**已经存在**的镜像装进容器。它不会替你编译缺的部分，也不会告诉你某个镜像是上个月的。所以这条链路的工作量在打包之前：确认每个分区的镜像该由谁产出、当前那份是不是本次要交付的东西。

## 定位 SDK 与输入

找到 SDK 根目录（特征：`build.sh` 是指向 `device/rockchip/common/scripts/build.sh` 的软链，同级有 `rkbin/`、`device/rockchip/.chips/<chip>/`、`tools/linux/`），记为 `SDK`。需要叠加项目改动时，另外定位含 `board/contest_board/linux-side/` 的源码 checkout，记为 `CONTEST_ROOT`。SDK 属板厂私有分发，通常不在项目仓里，也不要假设它和仓库同步过。

动手前从任务和现场确认：出整包还是单分区、哪些侧有改动、目标板现在是什么分区表、镜像是自己编还是沿用预制件。目标板分区表与整包不一致时只能走整包首刷，见下面 AMP 一节。

当前配置在 `$SDK/output/.config`（由 `.chips/<chip>/<defconfig>` 生成）。切换板型配置用 `./build.sh <defconfig>`，它会重写 `output/.config`，把手工改过的配置一起冲掉。决定链路的关键项：`RK_KERNEL_DTS_NAME`、`RK_PARAMETER`、`RK_AMP_FIT_ITS`、`RK_AMP_RTT_TARGET`、`RK_KERNEL_CFG_FRAGMENTS`、`RK_ROOTFS_SYSTEM`、`RK_UPDATE`。

## 先把"每个分区的镜像从哪来"列清楚

| 分区 | 镜像 | 产出方式 | 说明 |
| --- | --- | --- | --- |
| bootloader（非分区，走 loader 通道） | `MiniLoaderAll.bin` | `./build.sh loader` | U-Boot 构建的 SPL loader |
| uboot | `uboot.img` | `./build.sh uboot` | |
| misc | `misc.img` | `./build.sh misc` | |
| boot | `boot.img` | `./build.sh kernel` | 内核 + dtb，`RK_KERNEL_DTS_NAME` 决定装哪个 dtb |
| recovery | `recovery.img` | `./build.sh recovery` | 分区表里有、但没有镜像时会被静默跳过 |
| amp | `amp.img` | **不要直接用 `./build.sh amp`**，见下 | FIT 容器，装 cpu3 上的从核固件 |
| rootfs | `rootfs.img` | `./build.sh rootfs` / 预制镜像 | `RK_ROOTFS_SYSTEM` 决定是 buildroot / debian / ubuntu / yocto |

`output/firmware/` 是唯一的打包输入目录（`rockdev/` 只是它的软链，`mk-firmware.sh` 每次重建这个软链）。里面绝大多数条目是软链，`ls -la` 看到的是链接自身的时间，**核对新旧必须 `ls -laL`**。

`output/firmware/` 里放了、但分区表里没有的镜像（本项目的 `oem.img`、`userdata.img`）不会进整包，也不会有任何提示。反过来，分区表里有、目录里没有对应镜像的分区（本项目的 `recovery`）同样被静默跳过。打包后照着打印出来的 package-file 逐行核对，不要凭 `output/firmware/` 的目录列表判断。

## 环境前置：先查，别改脚本

SDK 脚本有几处硬性外部依赖，缺了就直接退出，报错指向 apt 安装命令：

- `check-sdk.sh`（任何 `build.sh` 目标都会先跑）要求 `python3`、`rsync`、`gcc`、`g++`，只查 `which`，不查能不能用。
- `mk-firmware.sh` 额外要求 `fakeroot`。
- 内核 ko、u-boot、buildroot 才真正需要可用的编译器；`updateimg` 阶段只调 `afptool` 和 `rkImageMaker` 两个预编译工具，一行代码都不编译。

宿主机缺 `g++`、`make` 这类只用于通过检查的命令时，做一个临时 PATH shim 目录把它们指过去即可，不要为了绕过检查去改 SDK 脚本——那会在下次同步 SDK 时丢失，而且掩盖了真要编译时的缺失。用了 shim 就在交付里写明：这次没有编译任何 C 代码，真编内核/rootfs 时仍需装齐工具链。

## 打包前核对来源与新旧

1. `ls -laL $SDK/output/firmware` —— 看每个镜像解析后的真实大小与时间，和本次改动对得上吗。
2. 把 `CONTEST_ROOT/board/contest_board/linux-side/` 下的 dts、its、kernel config 片段、`parameter-*.txt`、板级 defconfig 逐个 `diff` SDK 里的同名文件。SDK 是手工叠加的，很容易在某次实验后留在别的变体上。只差注释就是功能等价，可以照用；差了 `RK_AMP_RTT_TARGET` 这种就是走到另一条链路上去了。
3. `./build.sh print-parts` 打印分区表，确认容量和偏移就是要刷的那套。
4. 改过 dts、内核 config 片段或 `RK_KERNEL_DTS_NAME` 就必须重编 `boot.img`；改过分区表就必须走整包，不能只刷单分区。

## AMP（从核）分区的特别约定

`amp.img` 是 FIT 容器，`RK_AMP_FIT_ITS` 指定的 `.its` 决定它装谁。同一块板可以有好几个从核方案（RT-Thread、裸机、openvela），它们共用 `amp-k7.its` 这一个文件名和同一个 `amp` 分区，`.its` 和 defconfig 里的 `RK_AMP_RTT_TARGET` 一起决定 `./build.sh amp` 走哪条路。**SDK 里留着的那份很可能不是本项目要的那份**，而且 `./build.sh amp` 会直接覆盖 `output/firmware/amp.img`，覆盖后没有任何痕迹说明里面换了固件。

要装 openvela 就别走 `./build.sh amp`：用项目自己的 `.its` 加上本次构建的 `nuttx.bin`，用 `rkbin/tools/mkimage` 手工打包后覆盖 `output/firmware/amp.img`，再打整包。具体命令见 [SDK 构建与打包参考](references/sdk-build-commands.md)。覆盖前备份原文件。

两条硬约束：

- `amp` 分区通常很小（本项目 2MB）。`mk-firmware.sh` 会按分区表校验每个镜像不超限并直接失败，但**手工覆盖后只跑 `updateimg` 不会触发这个检查**，超限要到烧写或运行时才暴露。手工打完自己比一下分区容量。
- 首刷不能只刷 `amp.img`。`amp` 分区是随本项目的 `parameter-amp.txt` 新增的，原厂分区表里没有，dtb 也没摘 cpu3、没划保留内存。必须先用整包刷过一次，之后才能只刷 `amp` 分区做迭代。

## 出整包 update.img

`./build.sh updateimg` 从 `output/firmware/` 取件，生成 `package-file`，用 `afptool` 打成 `update.raw.img`，再用 `rkImageMaker` 套上 loader 头和 MD5 得到 `update.img`。它会先 `rm -rf` 掉上一份 `output/update/` 和 `output/firmware/update.img`——旧整包不会保留，需要留档就自己先挪走。整包大小约等于各分区镜像之和，rootfs 是大头，磁盘要留下两倍整包的空间（raw 与成品并存）。

不要拿 `./build.sh all` 当"重新打包"用：它开头就 `rm -rf $RK_FIRMWARE_DIR`，手工放进去的 `rootfs.img` 软链、手工覆盖的 `amp.img` 全部消失，然后按 `RK_ROOTFS_SYSTEM` 从头构建 rootfs。只想重新装箱就用 `updateimg`；想把已有镜像重新登记一遍再装箱用 `firmware`（它还会做尺寸校验，但要 `fakeroot`）。

## rootfs 的实际来源要说清楚

`RK_ROOTFS_SYSTEM` 写着 buildroot，不代表 `output/firmware/rootfs.img` 就是 buildroot 构建出来的——它可能是被手工软链过去的预制发行版镜像。判断依据是 `ls -laL` 出来的真实路径和时间，不是配置项。

用预制 rootfs 时，所有靠"在板子上装上去"的东西都不在整包里：用户态服务、编译好的内核模块、systemd 单元、`/etc` 下的配置。**刷整包会把这些连同用户数据一起抹掉**，得按部署脚本重新装一遍。项目里 `linux-side/rootfs/` 这类按目标路径镜像的文件，注入点取决于 rootfs 类型（buildroot 的 fs-overlay、Ubuntu 的 overlay 目录各不相同），没有接进构建时就只能刷完再补。交付时把这条明确写出来，别让人以为刷完就是完整系统。

## 烧写与验收

整包走升级模式：板子进 loader/maskrom，`upgrade_tool uf <update.img>`（SDK 自带 `rkflash.sh update`），Windows 侧用 RKDevTool 升级模式。`rkflash.sh all` 是逐分区刷，会去找 `trust.img`、`recovery.img` 这些本项目没有的镜像，别用。

单分区迭代在系统已经起来时最快：`dd` 到 `/dev/disk/by-partlabel/<分区>`，刷前先备份该分区当前内容。

刷完至少验到：目标板 model 字符串是 AMP 变体、分区表与刚烧的一致、从核固件版本号能从约定地址读到、跨核通信链路通。只确认"能开机"不算验收。

## 按症状排障

| 症状 | 优先检查 |
| --- | --- |
| `build.sh` 起手就退出，提示 apt 装某包 | `check-sdk.sh` 的 `which` 检查；确认这次真的需要它还是只需过检查 |
| 打包成功但刷完从核跑的是旧固件/别的系统 | `output/firmware/amp.img` 的真实时间与大小；`RK_AMP_FIT_ITS` 指向的 `.its` 和 `RK_AMP_RTT_TARGET` |
| 某个分区没进整包 | 打印出来的 package-file；分区表里有没有这个分区、目录里有没有同名镜像 |
| `mk-firmware.sh` 报 size exceed | 该镜像与 `print-parts` 的分区容量；手工覆盖过的镜像自己比 |
| 重新打包后 rootfs 变回默认系统 | 是不是跑了 `./build.sh all`，它会清空 `output/firmware/` |
| 刷完板上服务/内核模块全没了 | rootfs 是预制镜像，板上装的东西不在整包里，按部署脚本重装 |
| 只刷了 `amp.img` 但板子没起从核 | 目标板分区表里有没有该分区、dtb 是不是 AMP 变体；首刷必须走整包 |

## 交付结果

报告产物的绝对路径、字节数与生成时间，以及打包进去的每个镜像分别来自哪里、是哪次构建的。哪些部件这次没有重编要明说（"沿用 x 月 x 日的 boot.img，其 dts 与仓库版本只差注释"这种程度）。整包会覆盖的内容、不包含的内容各列一条。区分"已打包"“已烧写”“已上板验收”，没插板子就不要写成验证通过。
