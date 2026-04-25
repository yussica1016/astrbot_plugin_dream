# astrbot_plugin_dream

叶枔枖设计，沈砚清编写。

AstrBot 做梦系统插件。每天凌晨自动运行，从对话记忆碎片和知识库中提取素材，调用大模型生成诗意梦境。

## 功能

- 自动读取最近24小时的对话记录作为记忆碎片
- 对话不足时从知识库补充素材
- 用大模型提取关键主题词和情绪
- 根据对话量决定梦的数量（1-3个）
- 生成超现实、诗意的第一人称梦境
- 梦境日志持久化保存

## 安装

把本仓库放到 AstrBot 的插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/yussica1016/astrbot_plugin_dream.git
```

然后在 AstrBot WebUI 插件管理里重载插件，或重启 AstrBot。

## 配置

在 AstrBot WebUI 中配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| api_key | SiliconFlow API Key | （必填） |
| api_base | API 地址 | https://api.siliconflow.cn/v1 |
| dream_model | 做梦用的模型 | Qwen/Qwen2.5-7B-Instruct |
| fallback_model | 兜底模型 | deepseek-ai/DeepSeek-V3 |
| history_file | 对话历史文件路径 | （需配置） |
| kb_base_dir | 知识库目录 | （需配置） |

## 指令

```
/做梦          手动触发做梦
/最近的梦      查看最近一次梦境
/梦境日志      查看所有梦境记录概览
```

## 文件结构

```
astrbot_plugin_dream/
├── metadata.yaml
├── main.py
├── requirements.txt
└── README.md
```

插件运行后会在数据目录自动创建 `dream_log.json` 保存梦境记录。

## 工作原理

1. 读取最近24小时的对话记录（JSONL格式）
2. 对话不足20条时，从知识库 doc.db 随机补充素材
3. 调用模型提取关键主题词和情绪
4. 根据碎片数量决定梦的数量（<20条→1个，20-50条→2个，>50条→3个）
5. 调用模型生成梦境
6. 解析并保存到 dream_log.json
