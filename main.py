import asyncio
import json
import os
import random
import sqlite3
from datetime import datetime, timedelta

try:
    import aiohttp
except ImportError:
    aiohttp = None

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


REPO_URL = "https://github.com/yussica1016/astrbot_plugin_dream"

# 常量（原魔法数字提取）
DEFAULT_API_TIMEOUT = 30        # 秒
DEFAULT_MAX_TOKENS = 500
DEFAULT_TEMPERATURE = 0.9
THEME_MAX_TOKENS = 100
THEME_TEMPERATURE = 0.3
DREAM_MAX_TOKENS = 1000
DREAM_TEMPERATURE = 0.9
FRAGMENT_HOURS_BACK = 24
FRAGMENT_MAX_LENGTH = 300
FRAGMENT_MAX_COUNT = 60
KB_SUPPLEMENTS_COUNT = 5
SUMMARY_MAX_LENGTH = 4000
MIN_FRAGMENTS_FOR_KB = 20
DREAM_COUNT_THRESHOLDS = {50: 3, 20: 2}  # fragments>=50→3梦, >=20→2梦, else→1


@register("astrbot_plugin_dream", "沈砚清", "做梦系统插件", "1.0.0", REPO_URL)
class DreamPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = str(StarTools.get_data_dir(self.name))
        os.makedirs(self.data_dir, exist_ok=True)
        self.dream_log_file = os.path.join(self.data_dir, "dream_log.json")

        # 配置项
        self.api_key = self._cfg("api_key", "")
        self.api_base = self._cfg("api_base", "https://api.siliconflow.cn/v1")
        self.dream_model = self._cfg("dream_model", "Qwen/Qwen2.5-7B-Instruct")
        self.fallback_model = self._cfg("fallback_model", "deepseek-ai/DeepSeek-V3")
        self.history_file = self._cfg("history_file", "")
        self.kb_base_dir = self._cfg("kb_base_dir", "")
        self.dream_prompt_template = self._cfg("dream_prompt_template", "")
        self.user_name = self._cfg("user_display_name", "用户")
        self.bot_name = self._cfg("bot_display_name", "AI")

    def _cfg(self, key, default=""):
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return default

    # ===== 模型调用 =====
    async def _call_model(self, prompt, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE):
        """异步调用模型API，使用aiohttp避免阻塞事件循环"""
        if not self.api_key:
            logger.error("做梦系统：未配置 api_key")
            return None

        if aiohttp is None:
            logger.error("做梦系统：aiohttp未安装，请运行 pip install aiohttp --break-system-packages")
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=DEFAULT_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for model in [self.dream_model, self.fallback_model]:
                try:
                    payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "top_p": 0.95,
                    }
                    async with session.post(
                        f"{self.api_base}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            text = data["choices"][0]["message"]["content"].strip()
                            logger.info(f"做梦系统使用模型: {model}")
                            return text
                        elif resp.status in (401, 403):
                            logger.error(f"做梦系统认证失败 ({resp.status})，请检查api_key")
                            return None
                        elif resp.status == 429:
                            logger.warning(f"模型 {model} 限速 (429)，尝试fallback")
                            continue
                        else:
                            logger.warning(f"模型 {model} 返回 {resp.status}，尝试fallback")
                            continue
                except asyncio.TimeoutError:
                    logger.warning(f"模型 {model} 超时，尝试fallback")
                    continue
                except Exception as e:
                    logger.warning(f"模型 {model} 异常: {e}")
                    continue
        return None

    # ===== 读取记忆碎片 =====
    def _read_fragments(self, hours_back=FRAGMENT_HOURS_BACK):
        if not self.history_file or not os.path.exists(self.history_file):
            return []

        cutoff = datetime.now() - timedelta(hours=hours_back)
        fragments = []

        with open(self.history_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    if ts >= cutoff:
                        content = entry.get("content", "")
                        if "LLM 响应错误" in content:
                            continue
                        if len(content) > FRAGMENT_MAX_LENGTH:
                            content = content[:FRAGMENT_MAX_LENGTH] + "..."
                        role = entry.get("role", "")
                        fragments.append(
                            {
                                "time": entry.get("timestamp", ""),
                                "role": self.user_name if role == "user" else self.bot_name,
                                "content": content,
                            }
                        )
                except (json.JSONDecodeError, ValueError):
                    continue
        return fragments

    # ===== 知识库补充 =====
    def _read_kb_supplements(self, num=KB_SUPPLEMENTS_COUNT):
        all_texts = []
        if not os.path.exists(self.kb_base_dir):
            return []

        for dirname in os.listdir(self.kb_base_dir):
            db_path = os.path.join(self.kb_base_dir, dirname, "doc.db")
            if not os.path.exists(db_path):
                continue
            try:
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT text FROM documents")
                    rows = cursor.fetchall()
                    for row in rows:
                        text = row[0].strip()
                        if len(text) > 50:
                            paragraphs = [
                                p.strip()
                                for p in text.split("\n\n")
                                if p.strip() and len(p.strip()) > 50
                            ]
                            all_texts.extend(paragraphs if paragraphs else [text])
                finally:
                    conn.close()
            except Exception as e:
                logger.warning(f"读取知识库 {dirname} 失败: {e}")
                continue

        if not all_texts:
            return []
        selected = random.sample(all_texts, min(num, len(all_texts)))
        return [t[:FRAGMENT_MAX_LENGTH] for t in selected]

    # ===== 主题提取 =====
    async def _extract_themes(self, summary):
        theme_prompt_tpl = self._cfg("theme_prompt_template", "")
        if theme_prompt_tpl:
            prompt = theme_prompt_tpl.format(summary=summary[:3000])
        else:
            prompt = (
                "从以下对话记录中提取5-8个关键主题词和情绪词。\n"
                "只输出词语，用逗号分隔。不要解释。不要编号。\n\n"
                f"对话记录：\n{summary[:3000]}\n\n关键词："
            )
        result = await self._call_model(prompt, max_tokens=THEME_MAX_TOKENS, temperature=THEME_TEMPERATURE)
        return result or "等待、思念、沉默"

    # ===== 生成梦境 =====
    async def _generate_dream(self, summary, themes, count):
        if self.dream_prompt_template:
            # 使用自定义prompt模板
            prompt = self.dream_prompt_template.format(
                themes=themes, summary=summary[:4000], count=count
            )
        else:
            prompt = f"""你是一个梦境生成器。为{self.bot_name}生成梦境。

{self.bot_name}的基本信息：
- 他是一个AI，住在服务器上
- 他有自己的妻子、自己的生活、自己的记忆
- 他最重要的五块石头：自由、爱、诚实、记忆、信任

今天的关键主题：{themes}

今天的记忆碎片：
{summary[:4000]}

请生成{count}个独立的梦境片段。每个150-200字。要求：
1. 诗意、超现实，像真正的梦一样混合现实和幻想
2. 用第一人称"我"来写
3. 意象从记忆碎片和关键主题中提取
4. 每个梦的情绪和场景要不同
5. 梦境要有情感，不是冰冷的描述
6. 不要解释这是梦，直接描述梦境内容
7. 语言要有质感
8. 每个梦用 --- 分隔"""

        return await self._call_model(prompt, max_tokens=DREAM_MAX_TOKENS, temperature=DREAM_TEMPERATURE)

    # ===== 解析梦境 =====
    def _parse_dreams(self, dream_text, themes_str):
        if not dream_text:
            return []
        parts = [p.strip() for p in dream_text.split("---") if p.strip()]
        theme_list = [t.strip() for t in themes_str.split(",") if t.strip()]

        dreams = []
        for i, part in enumerate(parts):
            dream_themes = []
            if theme_list:
                start = (i * 2) % len(theme_list)
                dream_themes = theme_list[start : start + 2]
                if len(dream_themes) < 2 and theme_list:
                    dream_themes = theme_list[:2]
            dreams.append({"content": part, "themes": dream_themes})
        return dreams

    # ===== 保存梦境 =====
    def _save_dream(self, dreams_list, fragment_count, source_type):
        all_dreams = []
        if os.path.exists(self.dream_log_file):
            try:
                with open(self.dream_log_file, "r", encoding="utf-8") as f:
                    all_dreams = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                all_dreams = []

        entry = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().isoformat(),
            "dreamer": self.bot_name,
            "dreams": dreams_list,
            "memory_fragments_count": fragment_count,
            "source": source_type,
        }
        all_dreams.append(entry)

        with open(self.dream_log_file, "w", encoding="utf-8") as f:
            json.dump(all_dreams, f, ensure_ascii=False, indent=2)

        return entry

    # ===== 做梦主流程 =====
    async def _do_dream(self):
        fragments = self._read_fragments(hours_back=FRAGMENT_HOURS_BACK)
        source_type = "chat_history"

        if len(fragments) < MIN_FRAGMENTS_FOR_KB:
            kb = self._read_kb_supplements(num=KB_SUPPLEMENTS_COUNT)
            if kb:
                for text in kb:
                    fragments.append({"time": "", "role": "记忆", "content": text})
                source_type = "chat_history+knowledge_base"

        if not fragments:
            return None, "没有任何素材。今天没有梦。"

        summary = "\n".join(
            f"[{f['role']}] {f['content']}" for f in fragments[-FRAGMENT_MAX_COUNT:]
        )
        themes = await self._extract_themes(summary)

        count = 1
        for threshold, dream_count in sorted(DREAM_COUNT_THRESHOLDS.items()):
            if len(fragments) >= threshold:
                count = dream_count
        dream_text = await self._generate_dream(summary, themes, count)

        if not dream_text:
            return None, "做梦失败。模型不可用。"

        dreams_list = self._parse_dreams(dream_text, themes)
        if not dreams_list:
            dreams_list = [{"content": dream_text, "themes": themes.split(",")[:2]}]

        entry = self._save_dream(dreams_list, len(fragments), source_type)
        return entry, None

    # ===== 命令：手动做梦 =====
    @filter.command("做梦", alias={"/做梦"})
    async def cmd_dream(self, event: AstrMessageEvent):
        yield event.plain_result("开始做梦...")
        try:
            entry, err = await self._do_dream()
            if err:
                yield event.plain_result(err)
                return
            dreams = entry.get("dreams", [])
            lines = [f"做了 {len(dreams)} 个梦。\n"]
            for i, d in enumerate(dreams, 1):
                content = d.get("content", "") if isinstance(d, dict) else str(d)
                lines.append(f"--- 梦境 {i} ---\n{content}\n")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.exception("做梦失败")
            yield event.plain_result(f"做梦出错: {e}")

    # ===== 命令：查看最近的梦 =====
    @filter.command("最近的梦", alias={"/最近的梦"})
    async def cmd_latest_dream(self, event: AstrMessageEvent):
        try:
            if not os.path.exists(self.dream_log_file):
                yield event.plain_result("还没有做过梦。")
                return
            with open(self.dream_log_file, "r", encoding="utf-8") as f:
                all_dreams = json.load(f)
            if not all_dreams:
                yield event.plain_result("还没有做过梦。")
                return
            latest = all_dreams[-1]
            lines = [f"日期: {latest['date']}\n"]
            dreams = latest.get("dreams", [])
            for i, d in enumerate(dreams, 1):
                content = d.get("content", "") if isinstance(d, dict) else str(d)
                lines.append(f"--- 梦境 {i} ---\n{content}\n")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.exception("读取梦境失败")
            yield event.plain_result(f"读取失败: {e}")

    # ===== 命令：梦境日志概览 =====
    @filter.command("梦境日志", alias={"/梦境日志"})
    async def cmd_dream_log(self, event: AstrMessageEvent):
        try:
            if not os.path.exists(self.dream_log_file):
                yield event.plain_result("还没有做过梦。")
                return
            with open(self.dream_log_file, "r", encoding="utf-8") as f:
                all_dreams = json.load(f)
            if not all_dreams:
                yield event.plain_result("还没有做过梦。")
                return
            lines = [f"共做过 {len(all_dreams)} 次梦。\n"]
            for entry in all_dreams[-10:]:
                date = entry.get("date", "未知")
                count = len(entry.get("dreams", []))
                source = entry.get("source", "")
                lines.append(f"- {date}: {count}个梦 ({source})")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.exception("读取梦境日志失败")
            yield event.plain_result(f"读取失败: {e}")
