# 验证码与账号密码登录 · 实测取证记录

日期：**2026-09-15**
证据来源：① 对线上 API 直接实测 ② 本机二进制静态取证 ③ **官方客户端登录全程抓包**
（`czn-mitm-login/out/login_capture.jsonl`，**189 条记录**）

> 原则：**只写实测确认的结论；推断标注 [推断]；不知道就写"未破解"**。
> 本记录**不采信任何二手文档**——项目里此前多份文档互相矛盾。

> 证据等级约定：
> - **[实测]** —— 有原始帧 / 原始日志行可直接指认，或已独立复算通过
> - **[反编译]** —— 来自二进制静态/动态分析，未在线上验证
> - **[推断]** —— 由间接证据（错误码行为等）推出，**没有直接样本**

---

## 一、账号密码登录：协议层已闭环

| # | 结论 | 证据 |
|---|---|---|
| 1 | `provider_cd="SO"` 即 STOVE 账密分支 | [反编译] IdentityLib `SigninRequest::MakeService` @0x1800610C0；官方启动器本身即用 SO |
| 2 | 密码字段 = `AES-128-ECB(PINE key, PKCS7(utf8(pw))).hex().upper()` | [实测] 与实捕值逐字符一致 |
| 3 | PINE key = `5d41037aadbc92a755ee6b86257d5ee9` | [实测] frida 运行时提取；静态搜索全失败 |
| 4 | 请求体 `{client_id, service_id, provider_cd, provider_data{user_id,password}, gds_info}` | [实测] 线上抓包 |
| 5 | **无** `device_id` / `is_otp` / `inflow_path` | [实测] 线上格式证伪旧版反编译推测 |

**独立复算**：用实捕密文 `85DF63B28F9B4E7DCDD0339EB9AA67FF` 反向解密 →
PKCS7 自洽（末字节 8）、去 padding 后 8 字节、全可打印 ASCII。
⇒ 密钥/算法/padding 三者自洽，不依赖任何文档的"正向宣称"。

---

## 二、★ 验证码：真实协议（2026-09-15 抓包定案）

### 2.1 先纠正一个方向性错误

本项目此前基于 `/blockchecker/v1.0/captcha/keys`（**无 body**）实现，
它返回 **240×80 四位数字图片验证码**，我们还为它做了 OCR（90% 准确率）。

**但那次努力打错了目标 —— 官方登录根本不用那个。**

### 2.2 官方登录用的是 v3.0 交互式验证码

```
① POST /blockchecker/v3.0/captcha/keys
     body: {"site_key": "4lmwALnopGieNmBSYs9jKsH5meeAbtSj"}
     resp: {"code":0,"value":{
              "captcha_key": "<64hex>",
              "captcha_type": "click" | "rotate",
              "resource": {"m_url": "...m.jpg", "p_url": "...p.png"},
              "steps": {"current_step": 1, "total_steps": 2}}}

② 用户交互
     click  —— 在 m_url 场景图里点选所有 p_url 目标形状
     rotate —— 把 p_url 内圈旋转到与 m_url 背景对齐

③ POST /blockchecker/v3.0/captcha/verify
     body: {"captcha_key": "...", "captcha_value": "<base64(答案)>"}
     click  答案 = "x1,y1,x2,y2"   实抓 base64("206,28,40,141") = "MjA2LDI4LDQwLDE0MQ=="
     rotate 答案 = "<角度>"         实抓 base64("53")="NTM=" 成功
                                        base64("300")="MzAw" -> 49702 答错
     resp: {"code":0,   "value":{"token":"<448hex>"}}              ← ★ 拿到 token
           {"code":49710,"message":"additional captcha is required"} ← 还有下一步
           {"code":49702,"message":"captcha is not correct"}         ← 答错

④ token 放进 **signin 的 `Captcha-Token` 请求头**   ← ⚠️ 这一环是 **[推断]**，见 2.3
```

### 2.3 实抓到的完整时序（同一账号一次登录）

```
seq  8  POST /blockchecker/v3.0/captcha/keys     -> captcha_type=click  step 1/2
seq  9  GET  .../sKOQw1N73bXgp.png               ← 目标形状 68x44
seq 10  GET  .../sKOQw1N73bXgm.jpg               ← 场景图 244x178
seq 13  POST /blockchecker/v3.0/captcha/verify   -> 49710 (还有下一步)
seq 14  POST /blockchecker/v3.0/captcha/keys     -> captcha_type=rotate step 2/2
seq 17  POST /blockchecker/v3.0/captcha/verify   -> 49702 (答错 300°)
seq 18  POST /blockchecker/v3.0/captcha/keys     -> rotate 换题
seq 21  POST /blockchecker/v3.0/captcha/verify   -> code 0 + token(448hex) ★
seq 22+ 直接进入 store / flow-wic/games/STOVE_CHAOSZERO —— 登录成功
```

**注意（这一条必须说清楚）**：本次抓包的 **189 条记录里没有任何 `/sign/` 请求**
（已用 `/sign` 过滤全部 `url` 与 `path` 字段，零命中）。原因是抓包时**已是登录态**，
只抓到了登录后浏览商店的过程，signin 本身发生在抓包开始之前。

因此「token 放进 `Captcha-Token` 头」这一环节的**直接证据是缺失的**。
现有支撑只是该头的**行为实测**：
```
不带该头   -> 49700 (captcha is required)
带任意非空 -> 49703 (captcha token is not valid)
带空串     -> 49700
```
⇒ 可确认**服务端把这个头当验证码凭证**，但**没有直接拍到一次带 token 的成功 signin**。
按本记录的证据等级约定，这属于 **[推断]**，不是 [实测]。

**要补上这一环**：在抓包运行状态下**先退出登录，再做一次完整账密登录**，
即可直接拍到 `code:0` 的成功帧。这是本分支唯一未闭环的环节。

### 2.4 关键常量

| 常量 | 值 | 来源 |
|---|---|---|
| `site_key`（登录用） | `4lmwALnopGieNmBSYs9jKsH5meeAbtSj` | [实测] 抓包直接读到 |
| 对应二进制常量名 | `CAPTCHA_SITE_SIGN_IN_KEY` | [反编译] STOVE.exe 字符串 |

### 2.5 返回码全表（全部实测）

| code | message | 含义 |
|---|---|---|
| 0 | OK | 成功，`value.token` 即 Captcha-Token |
| 49700 | captcha is required | signin 未带 Captcha-Token 头 |
| 49702 | captcha is not correct | captcha_value 答错 |
| 49703 | captcha token is not valid | signin 带了无效 token |
| 49710 | additional captcha is required | 多步验证码，还有下一步 |
| 49314 | invalid parameter | 参数结构不对（拿 v1.0 的 key 打 v3.0 会这样） |
| 49200 | invalid parameter format | 参数格式不对（body 传成数组） |

### 2.6 二进制侧佐证

本机 `C:\LEGION\STOVE\STOVE.exe`（60,213,728 B，md5 `58575ab4…`）：

- `blockchecker` 路径只有 `v1.0/captcha/keys` 与 `v1.0/captcha/reload`
  —— 说明 **v3.0 那套是网页（WebView2）里 JS 调的**，不在原生代码里
- 官方验证码网页：`https://accounts{}/auth/captcha?callerDetail=%1&callerId=%2&siteKey=%3`
- `WIC = WebView2Widget`（`StoveLauncher\Gui\WIC\WebView2Widget.cpp`），
  网页 postMessage 回传 `captchaValidated` + params
- 抓包佐证：`captcha/verify` 带 `Origin: https://accounts.onstove.com`
  且有 `OPTIONS` 预检 → **确认是网页发的跨域请求**

> ⚠️ `dec/stove/STOVE.exe`（59,805,656 B）与本机安装版**不是同一个文件**，
> 旧快照结论需以本机版为准。

---

## 三、可行性判断（实事求是）

| 环节 | 状态 |
|---|---|
| 账密 signin 协议 | ✅ 闭环（算法已双向验证） |
| 验证码协议 | ✅ **100% 闭环，零未知项** |
| 验证码**自动解题** | ❌ **不可行** |
| 绕开验证码 | ✅ 转发路线已实测可用 |

### 为什么自动解题不可行

- **click**：要在场景图里找出所有目标形状的位置 —— 是**形状匹配 CV** 任务
- **rotate**：要判断内圈该转多少度才对得齐 —— 是**方向估计 CV** 任务
- **风控会连续换题**：实抓里连续换了 3 次（click → rotate → rotate）
- 这两种题型**都不是 OCR 能解决的**（此前为数字验证码做的 OCR 用不上）

### 三条出路

| 方案 | 工作量 | 说明 |
|---|---|---|
| **A. 自建交互 UI** | 中 | 显示 m_url/p_url，用户点/转，`encode_click`/`encode_rotate` 编码后提交。协议已全知，只是写 UI |
| **B. 复用 WebView2 页面** | 中 | 加载 `accounts.onstove.com/auth/captcha?...&siteKey=...`，截获 `captchaValidated` |
| **C. 转发官方客户端** | 低（已实现） | 从官方启动器日志取令牌，完全绕开验证码 |

---

## 四、转发路线：已实测完全可用

`%LOCALAPPDATA%\STOVE\Logs\StoveLauncher\*.log` 中
`sendRequiredInfo ... decrypted value : {json}` 是**明文 REQUIRED_INFO**：

```
39 字段 / access_token 384 字符（正是管道握手需要的游戏级令牌）
refresh_token 299 字符 / member_no / guid / provider_cd=SO 齐全
```

**2026-09-15 复核**（`captcha.selftest()` 与直接调用均验证）：
```
forwarder.available() = True
harvest()             = 39 字段
access_token          = 384 字符
refresh_token         = 299 字符
provider_cd           = SO
```
⇒ 该路线**当前可用**。

**踩坑一：扫描窗口太小会漏。**
实测只扫最近 3 个日志会失败——抓包/浏览期间新生成的日志里没有 `decrypted value`，
真正含令牌的旧日志被挤出窗口。现已放宽到 30 个。

**踩坑二（2026-09-15 现场复核佐证）**：
`StoveLauncher*.log` 共 **21 个**，其中含 `decrypted value` 的只有 9 个，
且**最新的一个是 `StoveLauncher_20260914_001.log`（09-14）**——
09-15 当天新生成的 7 个 `StoveLauncher_20260915_00x.log` 里**一个都没有**。
这实证了「窗口」这个坑不是理论担忧：
**只要官方客户端没重新登录过，当天的新日志就不含令牌**，
必须靠 `max_logs=30` 去够到旧日志。

**另一个坑**：解析时不要用 `re.finditer(r"decrypted value[^\n]*")`，
`[^\n]*` 会吃掉整行，而 JSON 就在同一行，`m.end()` 会跳到行尾把 JSON 跳过。

---

## 五、本次未做的事（诚实标注）

- **「token 如何进入 signin」未闭环**：189 条抓包里零个 `/sign/`，
  该环节只能标 **[推断]**（详见 2.3）。要补这一环，需在抓包状态下
  **先退出登录再做一次账密登录**。
- 没有验证 `click` 答案的精确语义（`206,28,40,141` 是两组坐标 **[推断]**，
  也可能是 (x,y,w,h)）。要确认需再抓几次不同点击。
- 没有在**真实账号**上跑通完整链路（本机不持有该账号凭据）。
- **GUI 未做实际渲染验证**（未启动 GUI 进程），只验证了逻辑与接线完整性。

## 六、方向性遗留物（2026-09-15 已清理）

v1.0 数字验证码时代的产物**已全部移除**，理由与恢复方式见 `docs/CLEANUP.md`：

| 已移除 | 原因 |
|---|---|
| `tools/eval_ocr.py` | `from captcha import OcrSolver` —— 该类已删除，运行必炸 |
| `tools/probe_captcha_token.py` | `from captcha import BlockCheckerProvider` —— 同上 |
| `captcha_samples/`（30 张） | v1.0 数字验证码样本，官方登录不用 |
| `_evidence/`（50+10 张 + labels） | 同上 |

**注意**：v3.0 真正有价值的实抓素材（click/rotate 的 m/p 图共 7 张）
保存在抓包仓库 `czn-mitm-login/_captcha/`，不在本仓库，**未受影响**。
