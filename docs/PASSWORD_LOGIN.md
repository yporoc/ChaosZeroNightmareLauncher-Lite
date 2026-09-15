# 账号密码登录 —— 实现说明

> 实事求是版：**哪些是实测的、哪些是推断的、哪些做不到，全部标明。**
> 协议取证细节见 [`FINDINGS.md`](FINDINGS.md)。

---

## 一、这是什么

给本项目（原本只有**扫码登录**）新增**账号密码登录**能力，
核心是**人机验证码的处理与兜底转发**。

本功能开发在一个独立分支上，**主分支 `main` 的扫码方案一行未动**。

| 项 | 值 |
|---|---|
| 分支 | `feat/password-login` |
| 基线 | `main` @ `bac0335` |
| 原则 | **零改基线** —— 能力全部新增，原有代码逻辑不改 |

### 怎么回退

```bash
# ① 只放弃工作区改动（保留提交）
git checkout -- czn_lite.py gui.py config.json

# ② 回到功能之前
git checkout main
git branch -D feat/password-login
```

---

## 二、改动量（可验证）

| 文件 | 新增 | 删除 | 说明 |
|---|---|---|---|
| `czn_lite.py` | **+54** | **0** | 纯新增，原有代码一行没改 |
| `gui.py` | **+161** | **4** | 那 4 行只是二维码区从 4/5 行下移到 5/6 行，给新按钮腾位 |
| `config.json` | **+7** | **0** | 仅新增 `captcha` 段（1 个有效开关） |
| `captcha.py` | 新文件 | — | 验证码协议层 + 转发兜底 |
| `captcha_ui.py` | 新文件 | — | 交互式验证码窗口 |
| `tools/login_password.py` | 新文件 | — | 端到端 CLI（不启动 GUI） |
| `tools/check_ui_consts.py` | 新文件 | — | UI 常量引用防呆检查 |

> 若用 `diff` 直接比文件内容得到上千行差异，那是 `core.autocrlf` 造成的
> 行尾符干扰（仓库内存 LF，工作区检出 CRLF）。用 `git diff` 看才是真实的。

---

## 三、做了什么

### 3.1 `czn_lite.py`（+54 行，零删除）

**① 模块级：密码字段变换**

```python
PINE_ENCRYPT_KEY = "5d41037aadbc92a755ee6b86257d5ee9"

def stove_password_field(password, pine_key=PINE_ENCRYPT_KEY):
    """明文密码 -> 32 位大写 hex。"""
    key = bytes.fromhex(pine_key)
    if len(key) != 16:
        raise ValueError("PINE key 必须是 16 字节（32 hex）")
    data = password.encode("utf-8")
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_ECB).encrypt(data + bytes([pad]) * pad).hex().upper()
```

**② `StoveAuth.signin_password(user_id, password, captcha_token=None)`**

- 请求体与扫码同构，只把 `provider_cd` 换成 `"SO"`、`provider_data` 换成 `{user_id, password}`
- **复用**基类的 `self.s`（curl_cffi chrome 指纹会话）、`self._official_headers()`、
  `self._parse()`、`self._apply_launcher()`。**没有第二套 HTTP 层。**
- **返回响应 dict 而不抛异常** —— `49700`（需要验证码）是**正常中间态**，不是错误
- 本模块**不依赖** `captcha.py`，验证码由调用方传入

### 3.2 `captcha.py`（新文件）

| 组件 | 职责 |
|---|---|
| `CaptchaStep` | 一步验证码的数据（key / 题型 / 图片 URL / 步数） |
| `BlockCheckerV3` | 协议层：`fetch()` 取题、`download()` 取图、`submit()` 提交 |
| `encode_click` / `encode_rotate` | 答案编码（base64） |
| `CaptchaFlow` | 多步控制，自动处理 `49710` 继续取题 |
| `solve_login_captcha()` | 一站式入口：取题 → 交 UI → 提交 → 拿 token |
| `OfficialClientForwarder` | 兜底：从官方客户端日志取已登录令牌 |

### 3.3 `captcha_ui.py`（新文件）

交互式验证码窗口（customtkinter + PIL）：

- `click` 题型：在场景图上点选目标形状，显示红圈 + 序号
- `rotate` 题型：内圈旋转实时预览（旋转后贴合到背景白洞）+ 滑杆 + **图上按住拖动**

### 3.4 `gui.py`（+161/-4）

- 「账号密码登录」按钮（整行，二维码区下移）
- `_dialog_credentials()` 账号密码输入框（密码 `show=●`）
- `_ui_sync(fn)` worker 线程 → UI 线程桥接（Tk 控件只能在 UI 线程碰）
- `_task_pwdlogin()` 完整流程（含 49700 分支与兜底）
- `_show_captcha()` / `_show_captcha_1()` 验证码窗口调用

### 3.5 配置

`config.json` **只新增一个被代码真正读取的键**：

| 键 | 作用 | 读取方 |
|---|---|---|
| `captcha.forward_to_official_client` | 是否启用官方日志转发兜底（默认 `true`） | `captcha.OfficialClientForwarder.enabled()` |

其余协议常量（`site_key` / 端点 / 答案编码 / `pine_key`）**一律硬编码在代码里**，
不放配置 —— 它们不是「可调参数」，改了只会让功能坏掉。

---

## 四、完整流程

```
用户点「账号密码登录」
    │
    ├─ 弹输入框，取 (user_id, password)
    │
    ├─ auth.signin_password(uid, pw)
    │     │
    │     ├─ code=0     → ✓ 登录成功，写回 state.json
    │     ├─ code=49700 → 需要验证码，进入下一步
    │     └─ 其他 code  → 报错（49702 通常是账号或密码错）
    │
    └─ 49700 分支：solve_login_captcha(auth, ui_ask=...)
          │
          ├─ 取题 → 弹窗口 → 用户作答 → 提交
          │     ├─ code=0     → ✓ 拿到 token（448 hex）
          │     ├─ 49710      → 还有下一步，自动继续取题
          │     └─ 49702      → 答错，重来
          │
          └─ auth.signin_password(uid, pw, captcha_token=token)
                ├─ code=0    → ✓ 登录成功
                └─ code=49703 → token 未被接受（见第六节）
```

**兜底线路**（验证码过不去时）：

```
OfficialClientForwarder.harvest()
    └─ 从 %LOCALAPPDATA%\STOVE\Logs\StoveLauncher\*.log 取
       sendRequiredInfo ... decrypted value : {json}
       → 39 字段完整 REQUIRED_INFO（access_token 384 字符）
       → 直接喂管道握手，完全绕开验证码
```

---

## 五、哪些是实测的

| 项 | 证据等级 |
|---|---|
| `provider_cd="SO"` 是账密分支 | [实测] 官方启动器本身即用；IdentityLib 反编译佐证 |
| 密码变换算法 | [实测] 与实捕值逐字符一致 + **反向复算**（PKCS7 自洽、8 字节、全可打印） |
| signin 请求体结构 | [实测] 线上抓包 |
| `Captcha-Token` **是**验证码通道 | [实测] 不带→49700；带非空→49703；带空串→49700 |
| `Captcha-Token` 中**放有效 token 即成功** | **[推断]** 见第六节 |
| 验证码 keys 协议 | [实测] 抓包完整记录 |
| 验证码 verify 协议 + base64 答案 | [实测] 抓包记录 3 次提交，含一次成功返回 448 字符 token |
| `encode_click` / `encode_rotate` | [实测] 对着实捕值验算通过（`MjA2LDI4LDQwLDE0MQ==` / `NTM=` / `MzAw`） |
| 转发路线（官方日志取令牌） | [实测] 39 字段、access_token 384 字符 |

---

## 六、哪些是**推断**的（诚实标注）

### ★ 唯一的推断：token 放进哪个头

「把 `value.token` 放进 `Captcha-Token` 请求头」这一步**没有直接拍到**。
抓包时启动器已是登录态，**从未发出 signin 请求**（189 条记录里
零个 `/sign/` 路径）。

**推断依据**：
- 不带该头 → `49700 captcha is required`
- 带任意非空 → `49703 captcha token is not valid`
- 带空串 → `49700`（服务端视为未提供）

⇒ 服务端**确实把这个头当作验证码凭证**，所以有效 token 放那里**应当**被接受。

**若运行报 `49703`**，说明推断有误，需要：
1. 在抓包状态下**先退出登录再账密登录一次**，直接拍到成功样本
2. 或试其他位置（body 字段 / 别的头名）

### 其他未定项

| 项 | 状态 |
|---|---|
| `click` 答案精确语义 | `206,28,40,141` 是两组坐标 [推断]，也可能是 (x,y,w,h) |
| `site_key` 归属 | 二进制里有两个候选常量，无法确定哪个是登录用的 |
| `pine_key` 版本绑定 | IdentityLib 升级后可能失效，需重新提取 |
| 真实账号端到端 | 未跑过（开发环境不持有账号凭据） |
| GUI 实际渲染 | 未验证（只验了逻辑与接线） |

---

## 七、做不到的（直说）

**验证码无法自动识别。** 这两种题型都是 CV 任务：

- `click`：要在场景图里找出所有目标形状的位置 —— 形状匹配
- `rotate`：要判断内圈该转多少度才对得齐 —— 方向估计
- 风控还会**连续换题**（实抓里连续换了 3 次）

所以必须弹窗口让用户点一下。**OCR 用不上**（早期曾误以为是无 body 的
v1.0 数字验证码，做了 OCR 90%，后来实抓证明打错了目标，那套已全部删除）。

---

## 八、怎么验证

### 方式一：CLI（不启动 GUI，最快）

```bash
python tools/login_password.py        # 在仓库根目录执行
```

密码用 `getpass` 读入，**不回显、不打印、不落盘**；
`access_token` / `refresh_token` 只打印长度不打印值。

### 方式二：GUI

```bash
python gui.py
```
点「账号密码登录」→ 输入账号密码 → 若弹验证码窗口就作答。

### 结果判读

| 现象 | 含义 |
|---|---|
| 直接登录成功 | 无需验证码，链路正常 |
| 弹验证码窗口 | 正常流程，作答即可 |
| `49702` | 账号或密码错误 |
| `49703` | token 未被接受 —— 见第六节的推断问题 |
| 提示「转发也不可用」 | 官方客户端没登录过，日志里没有令牌 |

---

## 九、已知限制

- 密码变换的 PINE 密钥是**从二进制提取的常量**，STOVE 更新后可能失效
- 转发路线依赖**官方客户端登录过一次**（日志里才有令牌）
- 只在 Windows 上可用（整个项目都依赖 Win32 API）
