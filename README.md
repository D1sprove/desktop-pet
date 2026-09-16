# 桌面桌宠 Agent（Desktop Pet Agent）

一只住在桌面右下角的桌宠：**DeepSeek 流式对话 + 多人格 + 长期记忆 + 动画状态机 + 电脑使用时长统计 + 课表提醒 + 番茄钟**。

> 技术栈：Python 3.10+ / PySide6 / openai 兼容 SDK(DeepSeek) / psutil / pynput / APScheduler / PyYAML / SQLite(WAL)

---

## 一、快速开始

```bash
# 1. 安装依赖（建议 venv）
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置 API Key（三种方式任选其一，效果一样）
#   a) 图形界面：启动后右键宠物 →「⚙️ 设置」→ 填 Key → 可先点"测试连接"
#   b) 命令行  ：python main.py --set-key sk-你的key
#   c) 手动编辑：复制 .env.example 为 .env，填 DEEPSEEK_API_KEY=sk-你的key

# 3. 生成占位动画素材（首次运行前，若 assets 已有 GIF 可跳过）
python tools/gen_assets.py

# 4. 启动
python main.py
```

启动后宠物出现在桌面右下角：**左键单击** 拖拽移动，**双击** 打开数据面板，**右键** 弹出菜单，**托盘图标** 常驻。

### 其他入口

| 命令 | 说明 |
|---|---|
| `python main.py --selftest` | 无 GUI 自检：配置 / 数据库 / 记忆 / 采集 / 课表 / 番茄钟 / 素材 |
| `python main.py --bench 40` | 进程内实测 CPU / 内存（验收标准：CPU < 5%，内存 < 200MB） |
| `python main.py --smoke 8` | 离屏冒烟测试，运行 N 秒自动退出 |
| `python main.py --set-key sk-xxx` | 命令行把 Key 写入 .env 并退出 |
| `python tests/test_offline.py` | 15 项离线逻辑测试（不需要 API Key） |
| `python tools/screenshot.py` | 生成 `assets/preview_*.png` 预览图 |

实测结果（本机，offscreen 模式）：**CPU 平均 1.36%（单核口径 0.04%），内存峰值 70.4 MB，均达标**。

---

## 二、功能清单（对应 MVP Task 1-11）

| 模块 | 文件 | 功能 |
|---|---|---|
| 脚手架/配置 | `main.py` `app/utils.py` | config.yaml 加载（含 .env）、SQLite WAL 初始化、滚动日志 |
| API 接入设置 | `app/settings.py` | 图形化填 API Key / 接口地址 / flash·pro 模型 / 人格，支持一键"测试连接"；Key 写入 .env，其余写 config.yaml |
| 宠物窗口 | `app/pet_window.py` | 无边框/透明/置顶/不进任务栏；Alpha 遮罩实现"空白区域鼠标穿透"；拖拽 + 位置记忆；托盘菜单；独立气泡窗口（支持流式追加） |
| 流式对话 | `app/chat.py` | openai 兼容封装 DeepSeek；QThread 流式输出 → 气泡逐字显示；flash 闲聊 / pro 摘要 |
| 人格与上下文 | `app/chat.py` `config.yaml` | 内置 活泼/毒舌/学霸 三套 system prompt；上下文按 token 预算截断，超阈值用 pro 模型摘要压缩并持久化 |
| 长期记忆 | `app/memory.py` | 记忆表（偏好/事实/禁忌）；每 N 轮后台交给 pro 模型抽取偏好；新对话按关键词召回记忆拼入 system |
| 动画状态机 | `app/animations.py` | idle/thinking/speaking/happy/remind/working/break/sleep；QMovie 加载 GIF；空闲降帧（idle_fps）；无操作 30 分钟切瞌睡 |
| 使用时长采集 | `app/collector.py` | psutil 采前台进程/CPU/内存 + ctypes 取前台窗口标题 + pynput 键鼠活跃判定；按 浏览器/IDE/游戏/办公/聊天 分类；默认 10s 采样、攒 12 条批量写库 |
| 数据面板 | `app/panel.py` | 今日活跃时长、应用占比环形图（QPainter 手绘）、token 用量、最近记忆、今日课表、番茄钟状态与日志；CSV/JSON 导出 |
| 课表提醒 | `app/schedule_manager.py` | `data/courses.json` 导入（支持周次 `1-16` / `1,3,5`）；每分钟检查下节课，提前 15 分钟气泡 + 系统通知 |
| 番茄钟/提醒 | `app/pomodoro.py` | 25/5 分钟循环、4 轮长休、暂停/跳过、日志入库；通用提醒支持一次性与每日 |
| 优化收尾 | 全局 | 所有重活均在子线程（QThread / APScheduler / pynput），UI 只通过信号刷新；SQLite WAL + 批量写；README |

---

## 三、配置说明（config.yaml）

```yaml
api:
  base_url: "https://api.deepseek.com"
  api_key_env: "DEEPSEEK_API_KEY"   # key 存 .env，不进代码
  flash_model: "deepseek-chat"      # 闲聊（快）
  pro_model: "deepseek-chat"        # 摘要/抽取（可换 deepseek-reasoner）

personality:
  active: "lively"                  # lively / tsundere / scholar，也可自己加
pet:
  idle_fps: 3                       # 待机帧率（安静浮动）
  active_fps: 12                    # 说话/思考/提醒等互动时帧率
  sleep_after_minutes: 30           # 无操作切瞌睡
  mouse_transparent: true           # 空白区域鼠标穿透
collector:
  interval_seconds: 10              # 采样间隔（5-15）
  batch_size: 12                    # 批量写库阈值
  idle_threshold_seconds: 120       # 键鼠无操作视为空闲
schedule:
  remind_before_minutes: 15         # 课前提醒提前量
pomodoro: { work_minutes: 25, break_minutes: 5, cycles_before_long_break: 4 }
memory:   { summarize_every_turns: 16, recall_limit: 8 }
```

**隐私**：聊天记录、记忆、使用记录全部存本地 `data/pet.db`，不上传任何数据；唯一出网请求是调用 DeepSeek 对话接口。

---

## 四、目录结构

```
desktop-pet-agent/
├── main.py                  # 入口（装配所有模块 + --selftest/--bench/--smoke）
├── config.yaml              # 主配置
├── requirements.txt
├── README.md
├── app/
│   ├── pet_window.py        # 宠物窗口 / 气泡 / 聊天输入框 / 托盘
│   ├── chat.py              # DeepSeek 客户端 + 会话上下文 + 流式线程
│   ├── memory.py            # 记忆存储 / 关键词召回 / 异步偏好抽取
│   ├── collector.py         # 使用时长采集 + 统计查询
│   ├── panel.py             # 数据面板（含 QPainter 环形图 / 导出）
│   ├── schedule_manager.py  # 课表 + 课前提醒 + 通用提醒
│   ├── pomodoro.py          # 番茄钟
│   ├── animations.py        # 动画状态机
│   └── utils.py             # 配置 / 日志 / SQLite / token 估算
├── assets/
│   ├── animations/*.gif     # 各状态动画（tools/gen_assets.py 生成的占位素材）
│   └── icons/tray.png
├── data/                    # 运行时生成
│   ├── pet.db               # SQLite（WAL）
│   ├── courses.json         # 课表
│   └── app.log
├── tools/                   # gen_assets / gen_assets_from_image / bench / screenshot
└── tests/test_offline.py    # 离线单测（15 项）
```

---

## 五、替换成自己的宠物形象

两种方式：

1. **用一张角色立绘自动生成**（推荐，当前使用的就是这种方式）：

   ```bash
   python tools/gen_assets_from_image.py 你的图.png
   ```

   脚本会自动：去白底 → 连通域保留最大主体（去水印/杂块）→ 去除右上角对话气泡 →
   裁剪为 `assets/pet_base.png` → 按状态机生成 8 个状态 GIF（浮动/思考倾斜/说话压缩/跳跃/
   抖动/打瞌睡变暗等动作）→ 生成托盘图标。
   注意：去气泡的边界坐标是按当前这张立绘调的，换图后如残留气泡，微调
   `remove_bubble()` 里的斜线参数即可。

2. **手动放 GIF/APNG**：在 `assets/animations/` 下按状态名放同名文件
   （`idle.gif`、`thinking.gif`、`speaking.gif`、`happy.gif`、`remind.gif`、
   `working.gif`、`break.gif`、`sleep.gif`），无需改代码。
   窗口遮罩会按当前帧 alpha 自动重建，素材四周保留透明边距即可保证"空白区域可穿透"。

## 六、常见问题

- **启动后说"未配置 API Key"**：确认 `.env` 里 `DEEPSEEK_API_KEY=sk-...`，且文件在项目根目录。
- **想换人格**：右键宠物 → 🎭 切换人格；或在 config.yaml 里新增预设。
- **想导出数据**：双击宠物 → 面板「导出」页。
- **内存/CPU**：空闲时动画自动降到 `idle_fps`，采集批量写库；可用 `python main.py --bench 40` 复测。
