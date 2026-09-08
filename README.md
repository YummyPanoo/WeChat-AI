# WeChatRobot

微信公众号 AI 机器人后端。提供智能对话、图片分析、文生图等功能，支持用户订阅信息查询和微信绑定。

## 功能特性

- **智能对话**：基于通义千问（Qwen）的文本对话，支持多轮上下文
- **图片分析**：用户发送图片，AI 自动识别和分析图片内容
- **文生图**：根据用户描述生成图片（基于 ModelScope 魔搭 API）
- **用户信息查询**：读取 WeChatRegister 数据库，查询用户订阅/订购信息
- **微信绑定**：支持通过绑定码将微信号与网站账号关联
- **关注欢迎**：用户关注公众号时自动推送服务配额信息和注册链接

## 项目结构

```
WeChatRobot/
├── app/
│   ├── __init__.py           # Flask 应用工厂
│   ├── routes/
│   │   └── wechat.py         # 微信公众号消息处理路由
│   ├── services/
│   │   ├── ai_client.py      # AI 对话/图片分析/文生图 API 封装
│   │   ├── auth.py           # 微信签名验证 + API 调用
│   │   ├── message.py        # 客服消息发送（异步模式）
│   │   ├── session.py        # 用户会话历史管理
│   │   ├── media.py          # 媒体文件下载/上传
│   │   ├── pending.py        # 异步任务管理（同步模式重试机制）
│   │   ├── user_info.py      # 用户订阅/订购信息查询（只读）
│   │   └── handlers.py       # 消息处理器
│   ├── utils/
│   │   ├── logger.py         # 日志配置
│   │   └── xml_helper.py     # XML 消息构建
│   └── extensions.py         # 扩展配置
├── config/
│   └── settings.py           # 配置（读 .env）
├── run.py                    # 启动入口
├── requirements.txt          # Python 依赖
└── .env.example              # 环境变量示例
```

## 快速开始

### 1. 安装依赖

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，配置微信、AI 和数据库信息
```

### 3. 启动服务

```bash
python run.py                   # http://127.0.0.1:8080
```

### 4. 配置微信公众号回调

在微信公众号后台设置服务器配置：
- URL：`http://你的域名/wechat/callback`
- Token：与 `.env` 中 `WECHAT_TOKEN` 一致
- 消息加解密方式：明文模式（或兼容模式）

## 配置项（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| WECHAT_TOKEN | (空) | 微信回调 Token |
| WECHAT_APPID | (空) | 微信 AppID |
| WECHAT_APPSECRET | (空) | 微信 AppSecret |
| WECHAT_ASYNC | 0 | 是否启用客服消息异步模式（1=启用） |
| DASHSCOPE_API_KEY | (空) | 通义千问 API Key |
| QWEN_TEXT_MODEL | qwen-turbo | 文本对话模型 |
| QWEN_VL_MODEL | qwen-vl-plus | 视觉分析模型 |
| QWEN_CLASSIFY_MODEL | qwen-turbo | 意图分类模型 |
| MODELSCOPE_API_KEY | (空) | ModelScope 文生图 API Key |
| MODELSCOPE_T2I_MODEL | damo/text-to-image-synthesis | 文生图模型 |
| PORT | 8080 | 服务端口 |
| LOG_LEVEL | INFO | 日志级别 |
| DB_HOST | 127.0.0.1 | WeChatRegister 数据库主机 |
| DB_PORT | 3306 | WeChatRegister 数据库端口 |
| DB_USER | root | WeChatRegister 数据库用户名 |
| DB_PASSWORD | (空) | WeChatRegister 数据库密码 |
| DB_NAME | wechat_register | WeChatRegister 数据库名 |
| WECHAT_REGISTER_FRONTEND_URL | http://127.0.0.1:5173 | 注册登录购买页面地址 |

## 消息处理流程

### 同步模式（订阅号）

由于订阅号没有客服消息接口权限，系统采用同步 XML 回复模式：

1. 用户发送消息到公众号
2. 公众号将消息 POST 到 `/wechat/callback`
3. 系统启动后台 AI 处理，等待 4.8 秒
4. 如果 AI 完成，立即返回 XML 回复
5. 如果 AI 未完成，返回空响应触发微信超时重试
6. 最多可争取约 20 秒处理时间（首次 + 3 次重试 × 5 秒）

### 异步模式（服务号）

服务号有客服消息接口权限，系统会自动检测并切换：

1. 用户发送消息到公众号
2. 系统立即返回 "success"
3. 后台异步处理 AI 请求
4. 处理完成后通过客服消息接口推送结果

## 微信绑定功能

### 绑定流程

1. 用户在 WeChatRegister 网页端生成6位数字绑定码
2. 用户将此绑定码发送给公众号
3. 公众号自动识别6位数字码，调用 WeChatRegister 绑定接口
4. 绑定成功，用户的微信号与网站账号关联

### 关注后消息

用户关注公众号时，系统会：
- 检查该微信号是否已绑定用户账号
- 已绑定：显示用户名和订阅信息
- 未绑定：显示服务配额说明和注册链接

### 查询订阅信息

用户发送"我的订单"、"订阅"等关键词时：
- 已绑定用户：显示订阅状态、剩余免费次数、订单信息
- 未绑定用户：引导用户到网页端生成绑定码

## API 接口

### 微信回调

```
POST /wechat/callback
```

接收微信公众号推送的消息和事件。

### 健康检查

```
GET /
```

返回 `"WeChat AI Bot Running ✅"`。

## 依赖

- flask>=3.0
- requests>=2.31
- defusedxml>=0.7
- waitress>=2.1
- python-dotenv>=1.0
- pymysql>=1.1

## 与 WeChatRobot 的关系

```
┌─────────────────┐         ┌─────────────────┐
│   WeChatRobot   │         │ WeChatRegister  │
│   (公众号后端)   │         │  (注册管理系统)  │
└────────┬────────┘         └────────┬────────┘
         │                           │
         │  读取用户订阅信息（只读）   │
         │──────────────────────────>│
         │                           │
         │  调用绑定接口              │
         │──────────────────────────>│
         │                           │
         │                           │  MySQL 数据库
         │                           │<───────────────┐
         │                           │                │
```

- **WeChatRobot**：负责公众号消息处理、AI 对话、用户信息查询
- **WeChatRegister**：负责用户管理、服务购买、配额管理、微信绑定
- 两个系统共享同一个 MySQL 数据库 `wechat_register`
- WeChatRobot 以**只读**方式访问数据库，不修改任何用户数据
