# 椰子音乐 Coconut Music

本地优先的音乐播放器：歌单和设置在你自己电脑上，音频流从公开接口实时取，没有自建后端、没有账号系统、没有遥测。

> 个人学习项目。使用前请先读 [DISCLAIMER.md](DISCLAIMER.md)。

---

## 下载

**直接下成品，不用自己打包：**

👉 [**最新版 椰子音乐.exe**](https://github.com/Twdhsi-666/coconut-music/releases/latest)

Windows 单文件，双击即用，不需要装 Python。首次运行会在同目录建一个 `data\` 文件夹存歌单和设置。

手机端在 [`mobile/椰子音乐4.apk`](mobile/椰子音乐4.apk)。

---

## 是什么

- **本地优先**：歌单、收藏、播放历史、设置存在本机 JSON 里（原子写入 + 自动 `.bak` 备份），不上传。
- **秒开**：双击到窗口出现约 0.9 秒（冷启动实测），单文件 exe，不需要装运行库。
- **只取音频流**：从 B 站 DASH 里只要纯音频，不碰视频，本地服务代理 Range 请求所以能拖进度条。
- **导入歌单**：贴一张歌单分享链接，只把元数据搬过来；每首歌在第一次播放时才去匹配音源（懒匹配），不批量轰炸接口。
- **下载转 MP3**：音源是 AAC/MP4，下完自动转 320K CBR MP3，转不了就保留原文件并提示，不会假装成功。
- **可选登录**：扫码授权只为了拿更高音质，凭据只存本机，随时可清。

---

## 目录

```
pc/                         PC 端（Python，除 requests 外都是标准库）
├── app.py                  入口：单实例 / 本地服务 / Edge 应用窗口
├── server.py               HTTP 后端：API 路由、搜索匹配、音频流代理
├── bili.py                 搜索 + 纯音频流（含风控降级，见 MAINTAIN.md 第 2 节）
├── qqmusic.py              歌单元数据解析
├── downloader.py           下载 + AAC→MP3 转码
├── store.py                本地 JSON 数据库
├── share.py / linkparse.py 歌单分享文本、链接识别
├── hotkeys.py / paths.py   全局热键、路径管理
├── web/                    前端（原生 HTML/CSS/JS，无框架）
├── test_search.py          搜索链路自检（需要联网）
├── MAINTAIN.md             ★ 维护指南：为什么代码长这样、踩过哪些坑
└── 椰子音乐.spec            PyInstaller 打包配置

mobile/                     手机端单文件 WebView 应用 + 安装包
```

---

## 从源码跑

要求 Python 3.11+（开发环境 3.13），只需要 `requests`：

```bash
cd pc
pip install requests
python app.py
```

会自动在 `127.0.0.1:8765` 起本地服务并用 Edge 的应用窗口打开界面（只监听回环地址，局域网访问不到）。

想单独调后端就 `python server.py`；想验证搜索链路就 `python test_search.py`。

## 自己打包

```bash
cd pc
pip install pyinstaller
pyinstaller 椰子音乐.spec --noconfirm
```

产物在 `dist/椰子音乐.exe`。

**打包前建议**从 [Release 页面](https://github.com/Twdhsi-666/coconut-music/releases/latest) 下载 `ffmpeg.xz` 放到 `pc/` 下。
没有它也能打包，exe 照常能用，只是"下载后转 MP3"会降级成"保留原始 .m4a"并在界面上如实提示。

---

## 常见问题

**搜索报"风控拦截"**
B 站会间歇性拦脚本请求，跟你的账号和网络没关系。程序里已经做了三级降级（主接口 → 老接口 → 换匿名身份），
通常还能搜到；三条都失败就是真被拦了，等 1~2 分钟再试。连续快速搜同一个词最容易触发。

**下载下来的文件打不开**
检查是不是 `.m4a`——说明打包时没带 `ffmpeg.xz`。带上它重新打包，或者本机装个带 libmp3lame 的 ffmpeg。

**界面白屏**
`web/` 没打进包。检查 spec 里的 datas，或者直接下 Release 的成品。

**双击没反应**
程序已经在运行了，会把已有窗口切到前台，不会开第二个。

---

## 要接着改这份代码

先读 [`pc/MAINTAIN.md`](pc/MAINTAIN.md)。里面写了每个网络请求为什么那样写、风控的三种表现和应对、
Windows 上那几个绕不开的坑（WebView2 崩溃、任务栏图标、单实例），以及打包发布流程。
所有数字都是实测的，不是估计。

## 合规

本项目不存储、不再分发任何音频内容，音频流实时来自第三方平台公开接口，权利归原权利人所有。
下载功能与在线播放的法律性质不同，请谨慎使用。完整说明见 [DISCLAIMER.md](DISCLAIMER.md)。

## 许可证

[MIT](LICENSE)。仅覆盖本软件的源代码与构建产物，不覆盖通过本软件获取的任何第三方内容。
