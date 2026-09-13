# 素材工具链

`docs/` 下的海报与架构图都是**用代码生成的**，不是手改的二进制。这样可以：

- 改一处配色/文案就整批重出，不用担心漏改；
- 用**机器校验**替代"肉眼看"，防止文字溢出、错位、压边；
- 顺带把「这是什么视觉规范」写成可执行的事实（`svgkit.py` 里的调色板与字号）。

## 脚本

| 脚本 | 作用 |
|---|---|
| `svgkit.py` | 布局工具库：调色板、字体栈、卡片/胶囊/箭头/图标块；内置**保守的文字宽度估算**与 `allow` 包围盒断言，溢出会直接抛异常 |
| `gen_all.py` | 生成全部 SVG：logo、favicon、wordmark、poster、social-preview、产品架构图、技术架构图；同时输出 `boxes/*.json`（每个文本块的允许区域）供校验使用 |
| `check_svg.py` | 校验：XML 良构、`viewBox` 一致、**用真实 Chrome 的 `getBBox()` 逐个文本块比对估算值**、没有任何文字逃出画布 |
| `export_png.py` | 导出 PNG（把 Inter 以 base64 注入后渲染，保证 PNG 用的是品牌字体；提交的 SVG 刻意不内嵌字体，保持文件小） |
| `ink_check.ps1` | 像素级检查：墨迹比例、内容包围盒与四边留白、强调色是否出现、真实内容有没有被裁掉 |

## 用法

需要 Python 3.11，以及 Chrome 或 Edge（校验与 PNG 导出用；脚本会自动探测
`Program Files\Google\Chrome` 与 `Program Files (x86)\Microsoft\Edge`）。

```bash
cd docs/assets/tools

python gen_all.py ../../..      # 生成 SVG + boxes/*.json
python check_svg.py ../../..    # 43 项几何校验（真实字体度量）
python export_png.py ../../..   # 导出 PNG（含 @2x 海报与社交预览图）
powershell -File ink_check.ps1  # 像素级检查
```

参数是仓库根目录。`gen_all.py` 是**确定性**的：同样的输入必然产出逐字节相同的 SVG，
所以重跑之后 `git status` 应该是干净的。

## 改品牌 / 改文案

1. **配色与字体**：改 `svgkit.py` 顶部的常量（与前端 `web/src/styles.css` 的设计令牌保持一致）；
2. **仓库地址**：改 `gen_all.py` 里的 `GITHUB` 常量，然后重跑 `gen_all.py` + `export_png.py`
   （PNG 里也烤进了这行地址，所以必须一起重出）；
3. **文案与版式**：在 `gen_all.py` 里对应的 `gen_*()` 函数中调整，断言会立刻告诉你哪里放不下。

## 校验覆盖

- `gen_all.py`：每个文本块都带一个 `allow=(x0,y0,x1,y1)` 硬约束，**按保守估算**校验水平和垂直不溢出；
- `check_svg.py`：再用浏览器真实排版结果复核一遍（估算 vs 实测最大偏差可达 53px，
  说明估算确实是偏保守的，不会出现"估算过了但实际溢出"）；
- `ink_check.ps1`：真正的栅格检查 —— 海报四边留白实测为 88/84/90/48px，与设计一致，
  且没有任何真实内容被裁在画布外。
