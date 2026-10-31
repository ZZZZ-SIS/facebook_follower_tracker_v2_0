Facebook 专页粉丝增长监控 v2.0
================================

新增功能
--------
1. 自动识别本机 Chrome / Edge：
   - %LOCALAPPDATA%\Google\Chrome\Application\chrome.exe
   - %PROGRAMFILES%\Google\Chrome\Application\chrome.exe
   - %PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe
   - %LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe
   - %PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe
   - %PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe

2. 浏览器 / Facebook 账号板块：
   - 单账号固定使用：始终使用第一个勾选账号。
   - 多个账号交替使用：按专页顺序轮流分配给所有勾选账号。
   - 可以添加多个账号配置，即使它们使用同一个 Chrome/Edge 程序也可以。
   - 每个账号配置都使用独立资料目录：
     %LOCALAPPDATA%\FacebookFollowerTracker\browser_accounts\...

3. 账号登录：
   - 选中账号配置 -> 点击“登录选中账号” -> 在打开的浏览器中正常登录 Facebook。
   - 登录完成后直接关闭浏览器窗口，状态保存在该账号独立资料目录。
   - 不同账号配置可分别登录不同 Facebook 账号。

4. 浏览器运行方式：
   - 弹出显示：采集时看得到浏览器窗口。
   - 静默执行：headless 后台采集，不弹出浏览器窗口。
   注意：部分网站在无界面模式下返回的页面可能不同；如果静默模式无法正常读取，请切回“弹出显示”。

5. 多账号轮换示例：
   勾选账号1、账号2、账号3，选择“多个账号交替使用”：
   Page 1 -> 账号1
   Page 2 -> 账号2
   Page 3 -> 账号3
   Page 4 -> 账号1
   ...

6. 本版本不会绕过登录验证、验证码、账号限制或网站访问控制。

首次运行
--------
1. 解压整个 ZIP。
2. 双击 install_windows.bat。
3. 双击 start_windows.bat。
4. 在“浏览器 / Facebook 账号”区域检查自动识别结果。
5. 逐个选择账号配置，点击“登录选中账号”完成登录。
6. 选择单账号 / 多账号模式、弹出显示 / 静默执行，然后开始采集。

原有功能
--------
- 当前结果左侧大表格
- 自动专页名称 / 粉丝数采集
- 自定义采集/提醒间隔
- 增长阈值提醒
- 内置 / 自定义铃声
- 单专页本轮停止提醒
- 重新开始自动监控后恢复提醒
- 双击 ID / 专页名称复制
- CSV 导出
- SQLite 历史记录
- 自定义背景色 / Windows 暗色主题兼容

v6.6 浏览器启动修复
-------------------
1. 登录浏览器不再通过 Playwright launch_persistent_context 启动，而是直接调用本机 Chrome/Edge EXE。
2. 采集时先直接启动用户选中的 Chrome/Edge，并通过本机 CDP 调试端口让 Playwright 接管。
3. 修复 Windows Chrome/Edge 父进程快速退出导致“浏览器没有打开”的误判。
4. 浏览器启动失败时，会把更具体的错误显示在软件底部状态栏和当前结果状态列。
5. 每个账号仍使用独立 browser_accounts 资料目录；登录完成后请关闭该账号浏览器窗口，再开始自动采集。


v6.6 修复：采集按钮会预检/自动安装 Playwright；浏览器启动采用 CDP + persistent context 双通道；致命错误不会再被“本轮采集完成”覆盖。


v6.6 浏览器兼容修复
----------------------
- 采集前只清理本软件该账号资料目录残留的 Chrome/Edge 子进程，不影响日常浏览器。
- 自动清理 SingletonLock / DevToolsActivePort 等残留锁。
- 浏览器启动改为三层兜底：账号资料目录 CDP -> 临时登录资料副本 CDP -> 临时资料副本 persistent context。
- 临时资料副本会继承该账号的 Facebook 登录状态，用于避免资料目录锁死。
- 浏览器错误弹窗不再显示超长 Playwright 启动参数，只保留关键错误。
- Windows 浏览器日志增加编码兼容，减少乱码。


v6.6 多账号轮换修复
- 多账号模式改为跨轮次轮换。只有 1 个专页 ID 时，也会按“账号1 -> 账号2 -> 账号3 -> ...”在每一轮采集中切换。
- 多个专页时，同一轮内按专页顺序交替分配账号；下一轮从下一个账号开始。
- 采集状态栏会显示本轮账号分配，结果表“使用账号”列显示每个专页本次实际使用的账号。


v6.7 专页名称 / 粉丝数识别修复
------------------------------
1. 专页名称改为优先读取 og:title、浏览器标题、专页自身链接、主标题和头像名称。
2. 不再把“(20+)”“图片可能包含...”“公众人物主页”“聊天”“联系方式”等 Facebook UI/说明文字当成专页名称。
3. 粉丝数不再直接取页面里遇到的第一个数字，而是收集专页头部多个候选并按页面位置、文本长度和精确度评分。
4. 2.6万 / 3.2K / 1.1M 等缩写会强制按单位换算，例如 2.6万 = 26,000。
5. 如果网页只显示约值，状态列会明确显示“网页显示约值：2.6万位粉丝 ≈ 26,000”。


=== v6.8 Google 表格同步 ===
1. 在 Google Cloud 创建“服务账号”，下载 JSON 密钥。
2. 打开目标 Google 表格，把表格共享给 JSON 文件中的 client_email，并授予“编辑者”。
3. 确认该 Google Cloud 项目已启用 Google Sheets API。
4. 软件“当前结果”上方粘贴 Google 表格链接，填写工作表名称（默认：当前结果），选择 JSON。
5. 点击“写入 Google 表格”。
6. 可勾选“每轮后自动同步”，每轮采集完成后会用当前结果覆盖该工作表内容。

注意：软件不会上传或显示你的服务账号 JSON 内容；JSON 仅由本机 Google API 客户端读取。

【账号登录资料安全】
- 每个 Facebook 账号的持久登录状态保存在：%LOCALAPPDATA%\FacebookFollowerTracker\browser_accounts\
- 请不要以“管理员身份运行”本软件；正常使用不需要管理员权限。
- 不要共享、上传或备份 browser_accounts 目录到不可信位置。
- 如果某个账号不再使用：在“浏览器 / Facebook 账号”区域选中该账号，点击“删除登录资料”。这会删除该账号保存的 Cookie/会话，但保留账号配置，可稍后重新登录。


安全复审补充（2026-10-30）
----------------------------
- Google 服务账号 JSON 会先校验 type、client_email、private_key 标记，并且 token_uri 必须严格等于 https://oauth2.googleapis.com/token；被修改为第三方令牌端点的 JSON 会被拒绝。
- 浏览器自动识别只信任 Chrome / Edge 的标准安装目录，不再从 PATH 搜索 chrome.exe / msedge.exe，避免用户可控 PATH 引入任意程序。
- 采集产生的 runtime_profiles 临时浏览器资料会在正常结束时清理，程序启动时还会清理崩溃遗留副本；这些副本可能包含登录 Cookie，因此不要手工复制或共享该目录。
- 浏览器诊断日志每次启动都会截断，只保留当前会话，减少 URL/诊断信息长期残留。
- 当前仍保留“缺少 Playwright 时在线安装固定版本依赖”的便利功能。高安全发布场景建议改成离线 wheelhouse + SHA-256 哈希锁定，详见 SECURITY_AUDIT.md。
- Qt 6.12.0 在 2026-10-30 新披露 CVE-2026-105924；本项目未直接使用受影响的 QCborValue -> JSON 路径，但应在 PyQt6-Qt6 6.12.1 可用后升级。


【软件图标】
- 已使用 10M+ FOLLOWERS 蓝色增长图标。
- 蓝色边框外为透明区域。
- PyQt6 窗口、任务栏、系统托盘和 PyInstaller 生成的 EXE 均使用同一图标。
