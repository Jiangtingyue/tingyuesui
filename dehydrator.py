"""
脱水压缩器 (Dehydrator)
1. 长文本压缩成精炼记忆
2. 用 Russell 环形情感模型打标：valence(效价) + arousal(唤醒度)
3. 自动分类 domain
4. API 不可用时降级到本地关键词分析
"""
import json
import re
from config import MEMORY_CONFIG, COST_OPTIMIZATION_CONFIG

# ── 本地降级用的关键词表 ──
EMOTION_KEYWORDS = {
    "high_arousal_neg": {
        "words": ["害怕", "恐惧", "愤怒", "崩溃", "疼", "哭", "痛", "吵架",
                  "绝望", "焦虑", "恨", "伤心", "委屈", "噩梦", "打", "骂"],
        "valence": -0.7, "arousal": 0.85
    },
    "high_arousal_pos": {
        "words": ["开心", "兴奋", "惊喜", "哈哈", "太好了", "爱", "喜欢",
                  "幸福", "感动", "想你", "好棒", "结婚", "告白"],
        "valence": 0.8, "arousal": 0.8
    },
    "low_arousal_neg": {
        "words": ["累", "无聊", "失望", "孤独", "麻木", "没意思", "算了",
                  "放弃", "不想", "随便", "无所谓"],
        "valence": -0.5, "arousal": 0.25
    },
    "low_arousal_pos": {
        "words": ["平静", "安心", "舒服", "放松", "温暖", "陪", "抱",
                  "牛奶", "晚安", "安全"],
        "valence": 0.6, "arousal": 0.3
    },
}

DOMAIN_KEYWORDS = {
    "emotion": ["感觉", "心情", "情绪", "开心", "难过", "哭", "笑", "爱", "恨"],
    "health": ["吃药", "疼", "头疼", "失眠", "流鼻血", "医院", "身体"],
    "daily": ["今天", "早上", "吃饭", "洗澡", "出门", "回家", "天气"],
    "relationship": ["朋友", "妈妈", "爸爸", "家人", "吵架", "和好"],
    "creative": ["写", "小说", "游戏", "画", "设计", "剧本", "故事"],
    "dream": ["梦", "噩梦", "梦见", "鬼压床", "做梦"],
    "memory": ["以前", "小时候", "记得", "那时候", "从前"],
}


class Dehydrator:
    """记忆脱水压缩 + 情感打标"""

    def __init__(self):
        pass

    async def process(self, content: str, source: str = "chat") -> dict:
        """
        处理一条原始内容 → 返回压缩后的记忆 + 情感坐标
        优先用 API，失败降级到本地
        """
        min_remote_chars = int(COST_OPTIMIZATION_CONFIG.get("remote_enrichment_min_chars", 120) or 120)
        if MEMORY_CONFIG.get("use_remote_enrichment", False) and len(content.strip()) >= min_remote_chars:
            try:
                result = await self._api_process(content)
                if result:
                    return result
            except Exception as e:
                print(f"[Dehydrator] API 降级: {e}")

        # 默认本地处理：稳定、免费，也不会跟主聊天争抢 DeepSeek 并发。
        return self._local_process(content)

    async def _api_process(self, content: str) -> dict | None:
        """Optional compression/emotion enrichment on the DeepSeek Flash route."""
        from api_cost import auxiliary_chat

        result = await auxiliary_chat(
            messages=[{"role": "user", "content": self._build_prompt(content)}],
            purpose="memory_dehydrate",
            system_prompt="你是记忆压缩器。只返回JSON，不要其他内容。",
            max_output_tokens=400,
        )
        if not result:
            return None
        text = str(result.get("content") or "").strip()
        if text.startswith("```"):
            text = re.sub(r"```json?\s*", "", text)
            text = re.sub(r"```\s*$", "", text)
        value = json.loads(text)
        return value if isinstance(value, dict) else None

    def _build_prompt(self, content: str) -> str:
        return f"""分析以下内容，返回JSON格式：

{{
  "summary": "压缩成1-2句核心信息",
  "valence": 0.0,     // 效价 -1(极负面) 到 +1(极正面)
  "arousal": 0.0,     // 唤醒度 0(平静) 到 1(极强烈)
  "importance": 0.5,  // 重要性 0-1
  "domain": "分类",   // emotion/health/daily/relationship/creative/dream/memory/other
  "resolved": false   // 这件事是否已经解决
}}

内容：
{content[:1500]}"""

    def _local_process(self, content: str) -> dict:
        """本地关键词降级分析"""
        text = content.lower()

        # 情感分析
        valence = 0.0
        arousal = 0.5
        max_hits = 0

        for category, info in EMOTION_KEYWORDS.items():
            hits = sum(1 for w in info["words"] if w in text)
            if hits > max_hits:
                max_hits = hits
                valence = info["valence"]
                arousal = info["arousal"]

        # 域分类
        domain = "other"
        max_domain_hits = 0
        for d, keywords in DOMAIN_KEYWORDS.items():
            hits = sum(1 for w in keywords if w in text)
            if hits > max_domain_hits:
                max_domain_hits = hits
                domain = d

        # 重要性（长度 + 情绪强度 → 更重要）
        length_score = min(len(content) / 500, 1.0)
        importance = round(0.3 + length_score * 0.3 + abs(valence) * 0.2 + arousal * 0.2, 2)

        # 压缩（本地只做截断）
        summary = content[:200] + ("..." if len(content) > 200 else "")

        return {
            "summary": summary,
            "valence": round(valence, 2),
            "arousal": round(arousal, 2),
            "importance": min(importance, 1.0),
            "domain": domain,
            "resolved": False,
        }

    async def close(self):
        return None


# 全局单例
dehydrator = Dehydrator()
