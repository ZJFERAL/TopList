# TopList — 置顶待办清单

一个 Windows 桌面置顶待办小工具：把一个文件夹里的 Markdown 待办清单以清单形式常驻桌面显示，勾选任务直接写回 `.md` 文件。数据永远是纯文本 Markdown，可以用任何编辑器（VS Code、Obsidian、Typora…）同时编辑，TopList 会自动同步刷新。

![Tech](https://img.shields.io/badge/Python-3.9+-blue) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey) ![License](https://img.shields.io/badge/License-Apache%202.0-green)

## 功能特性

- **窗口置顶**：常驻桌面角落，钉在所有窗口之上，可随时取消置顶
- **Markdown 驱动**：只认任务行 `- [ ]` / `- [x]`，`#` 标题自动分组，支持 YAML frontmatter 元数据
- **双向同步**：在应用里勾选任务会写回 md 文件；在外部编辑器修改文件，应用自动刷新
- **多文件切换**：侧栏列出文件夹内的 `.md` 文件（按修改时间排序），支持进入子文件夹浏览
- **编辑模式**：直接增删改任务行，支持撤销/重做（Ctrl+Z / Ctrl+Y，每文件独立 20 步）
- **文件管理**：重命名、删除（移入回收站，可还原）、在资源管理器中定位、按模板新建 TODO
- **自定义模板**：把 `template.md` 放在 `TopList.exe` 旁边即可覆盖内置模板，`{date}` 占位符自动替换为当天日期
- **紧凑模式**：一键隐藏侧栏，只留任务区
- **无边框窗口**：顶栏拖拽移动、八方向边缘缩放、最小化/最大化，窗口位置和大小会被记住
- **编码兼容**：自动识别 UTF-8 / UTF-8 BOM / GBK，写回时保留原文件的 BOM 和换行风格

## 快速开始

需要 Python 3.9+ 和 Windows（依赖 WebView2 Runtime，Win10/11 一般自带）。

```bash
pip install -r requirements.txt
python main.py
```

启动后点击左上角 📁 选择存放待办 `.md` 文件的文件夹即可。

### 待办文件格式示例

```markdown
---
title: 本周计划
---

# 工作

- [x] 完成 TopList v1.0
- [ ] 写周报
  - [ ] 补充数据

# 生活

- 周三晚上跑步   ← 普通文本行原样显示
```

## 打包为 exe

使用 PyInstaller 单目录模式打包（WebView2 依赖较多文件，不建议 onefile）：

```bash
pip install pyinstaller
pyinstaller TopList.spec
```

产物在 `dist/TopList/TopList.exe`。

## 项目结构

```
├── main.py        # 入口：创建无边框置顶窗口，处理 Win32 窗口样式
├── server.py      # 本地 HTTP 服务：托管 web/ 页面 + /api/* JSON 接口
├── backend.py     # Api 类：配置、文件读写、窗口控制等后端逻辑
├── mdparser.py    # Markdown 解析：frontmatter / 标题分组 / 任务行
├── watcher.py     # watchdog 监听文件夹（防抖回调）
├── web/           # 前端（原生 HTML/CSS/JS，无框架）
├── template.md    # 新建 TODO 文件的默认模板（exe 旁可放同名文件覆盖）
└── TopList.spec   # PyInstaller 打包配置
```

## 技术要点

- **前端与后端通过本地 HTTP JSON API 通信**（`127.0.0.1` 随机端口），而不是 pywebview 的 `js_api` 桥——彻底规避了 `pywebviewready` 事件时序和 `evaluate_js` 回传的死锁问题
- **无边框窗口**没有子类化 WndProc 拦截 `WM_NCCALCSIZE`（会与 WebView2 初始化冲突导致卡死），而是启动后补上 `WS_THICKFRAME` 样式，并用 DWM 属性去掉边框颜色，缩放/移动通过转发 `WM_NCLBUTTONDOWN`（HTCAPTION / 边缘 hit-test）交给系统模态循环处理
- **文件变更检测**采用目录指纹（文件名 + mtime 哈希）轮询对比，配合 watchdog 防抖，自己写回的变更不会触发无谓刷新

## 配置

配置保存在 `%APPDATA%\TopList\config.json`（文件夹路径、当前文件、置顶/紧凑状态、窗口位置），运行日志在同目录 `log.txt`。

## License

[Apache License 2.0](LICENSE)
