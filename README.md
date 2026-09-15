# CZN Launcher Lite

Chaos Zero Nightmare（卡厄思梦境）国际服第三方极简启动器，支持全程免代理免加速直接裸连登录和启动游戏。

> **免责声明**：本项目为非官方第三方工具，与 Smilegate / STOVE 无任何关联。
> 仅供个人学习研究，禁止商业用途。使用本项目可能违反游戏服务条款并带来账号风险，
> 一切后果由使用者自行承担。

## 快速开始

**使用打包版**

1. 解压后确认 `config.json` 与 exe 位于同一目录
2. 运行 `czn-lite-gui.exe`
3. 点「获取离线信息」自动探测游戏安装路径
4. 点「扫码登录」，用手机 STOVE App 扫码
5. 点「启动游戏」

**从源码运行**

```bash
pip install -r requirements.txt
python gui.py          # 图形界面
python czn_lite.py     # 控制台版
```

## 两种登录方式

| 方式 | 说明 |
|---|---|
| **扫码登录** | 用手机 STOVE App 扫码。**推荐**，无需输入账号密码 |
| **账号密码登录** | 在启动器内输入 STOVE 账号密码。首次通常会要求做人机验证码 |

### 关于账号密码登录

STOVE 的账密登录在服务端会要求**交互式人机验证码**（点选形状 / 旋转对齐），
启动器会弹出一个窗口让你作答。这类验证码**无法自动识别**（属于 CV 任务），
必须人工点一下。

## 环境要求

- Windows 10/11 x64
- 已安装官方 STOVE 客户端与游戏本体（本启动器不替代游戏）
- 源码运行需要 Python 3.12

## 配置

全部配置集中在 `config.json`。所有配置信息和账号信息本地明文保存。

| 键 | 说明 |
|---|---|
| `game.install_root` | 游戏安装目录，或用「获取离线信息」自动探测 |
| `platform.caller_id` | 官方客户端版本串 |

`state.json` 由程序自动生成，内含登录凭据，**等同账号密码，切勿外传**。

## 构建

```
双击运行build.bat
```

## 许可

GNU General Public License v3.0，见 [LICENSE](LICENSE)。
