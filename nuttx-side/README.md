# nuttx-side/ — openvela 公共仓（nuttx）侧的 RK3576 芯片层补丁

本作品的板级代码在 `board/contest_board/`（经 manifest `<linkfile>` 注入
`vendor/openvela/boards/contest2026_290_board`），但它依赖的 **RK3576 芯片层**
与 **GICv2 AMP-slave 支持** 属于 openvela 公共仓 `nuttx`，无法通过 `<linkfile>`
注入，故以 patch 系列形式随作品提交。

- 基线：`openvela/nuttx` 的 `dev-ai-contest-2026` 分支，commit
  **`dd92bcf4257`**（"drivers/lcd/lcd_dev: Add poll support"）——即
  `openvela.xml` 中 `nuttx` project 所指向的版本
- 共 5 个提交、21 个文件、2235 行新增
- 已验证：在上述基线上 `git am` 全部应用成功，产出的 tree 与开发机上的
  工作树完全一致（`c1199896`）

## 应用方法

```bash
# 在 repo sync 出来的 openvela 工作区里
cd nuttx
git am ../contest2026_290_dairoot/nuttx-side/*.patch
cd ..
./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j$(nproc)
```

若基线已前移导致冲突，可用 `git am -3` 三方合并。

## 各 patch 内容

| Patch | 内容 |
|---|---|
| 0001 | 新增 `arch/arm64/{include,src}/rk3576` 芯片层（boot、addrenv、soc、UART5 serial）；`arm64_gicv2.c` 新增 `CONFIG_ARM64_GIC_SLAVE`：不初始化 distributor（归 Linux），只做 banked CPU interface 与自有 SPI 的 set-bit 操作 |
| 0002 | `rk3576_rptun.c` / `rk3576_rsctable.c`：rptun/OpenAMP remote 角色后端，静态资源表、固定地址 vring、`GICD_ISPENDR` 写位 notify |
| 0003 | 去掉调试桩，定稿 AMP-slave 芯片层与 rptun |
| 0004 | `rk3576_pdm.c` / `rk3576_sai.c`：PDM（v2 IP）与 SAI 麦克风采集前端 |
| 0005 | 修正 IOMUX 上下拉编码 |

## 与主线的关系

这些改动照 `rk3588`/`rk3399` 的模板实现，接口上是纯增量（新增 chip 目录 +
一个 `ARM64_GIC_SLAVE` Kconfig 开关），具备向 openvela 主线提 PR 的形态。
