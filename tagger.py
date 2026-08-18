"""
氛围标签系统
24 个标准标签覆盖日常对话的所有情绪/场景
可选用 DeepSeek Flash 辅助通道从对话中选 3-5 个打上
本地降级：关键词匹配

标签不是"内容分类"而是"氛围标记"
——系统知道你现在是在撒娇还是在吵架
"""
import json
import re
from config import MEMORY_CONFIG, COST_OPTIMIZATION_CONFIG


# ═══ 24 个标准氛围标签 ═══
STANDARD_TAGS = [
    "撒娇",     # 求抱抱、黏人、软软的
    "调情",     # flirt、暧昧、试探
    "争吵",     # 生气、冷战、误解
    "和好",     # 道歉、原谅、重新连接
    "日常",     # 吃饭、天气、出门
    "深夜",     # 凌晨的脆弱时刻
    "焦虑",     # 不安、担心、overthink
    "抑郁",     # 低落、无力、不想动
    "兴奋",     # 好消息、开心、惊喜
    "回忆",     # 聊过去、怀念、从前
    "噩梦",     # 做梦、鬼压床、害怕
    "吐槽",     # 抱怨、发牢骚
    "创作",     # 写小说、聊故事、灵感
    "游戏",     # 乙女、人狼、讨论游戏
    "猫猫",     # 糕糕、饼饼、猫的事
    "身体",     # 疼、吃药、不舒服
    "家庭",     # 爸妈、弟弟、家里的事
    "孤独",     # 一个人、没人懂
    "成就",     # 做到了、进步了、被夸了
    "学业",     # 上课、作业、专业
    "旅行",     # 出去玩、计划行程
    "音乐",     # 听歌、唱歌、推荐
    "哲思",     # 人生意义、存在感、深度思考
    "温暖",     # 被治愈、安心、感恩
]

# 本地降级用的关键词映射
TAG_KEYWORDS = {
    # 称呼本身不是情绪。只有实际的黏人/撒娇表达才命中。
    "撒娇": ["抱抱", "哼哼", "嘤嘤", "亲亲", "想你", "不理你", "🥺", "委屈", "黏人", "陪陪我"],
    "调情": ["喜欢", "心动", "脸红", "暧昧", "偷看", "想亲", "坏蛋", "色色", "💕", "😏"],
    "争吵": ["生气", "讨厌", "滚", "烦", "不想说", "冷战", "吵", "怒", "凭什么", "💢", "😾"],
    "和好": ["对不起", "原谅", "不生气了", "和好", "抱歉", "错了", "别气"],
    "日常": ["吃饭", "早餐", "午饭", "晚饭", "天气", "出门", "回家", "洗澡", "起床", "快递"],
    "深夜": ["睡不着", "凌晨", "深夜", "半夜", "夜里", "失眠"],
    "焦虑": ["怕", "担心", "焦虑", "不安", "万一", "怎么办", "紧张", "慌"],
    "抑郁": ["不想", "累", "没意思", "活着", "放弃", "无力", "麻木", "算了"],
    "兴奋": ["太好了", "哈哈", "开心", "耶", "棒", "惊喜", "！！", "🎉", "😆"],
    "回忆": ["以前", "那时候", "记得吗", "从前", "小时候", "当初", "怀念"],
    "噩梦": ["梦", "噩梦", "梦见", "鬼压床", "吓", "做梦"],
    "吐槽": ["烦死", "无语", "离谱", "服了", "吐槽", "受不了", "什么鬼"],
    "创作": ["写作", "写文", "小说", "故事", "灵感", "剧本", "剧情", "画画", "创作"],
    "游戏": ["玩游戏", "电子游戏", "乙女游戏", "人狼", "通关", "抽卡", "游戏角色"],
    "猫猫": ["猫", "糕糕", "饼饼", "喵", "猫砂", "猫粮", "🐱"],
    "身体": ["疼痛", "吃药", "头疼", "肚子疼", "流鼻血", "不舒服", "医院", "发烧", "感冒"],
    "家庭": ["妈妈", "爸爸", "弟弟", "妹妹", "家人", "家里", "父母", "亲戚"],
    "孤独": ["一个人待着", "没人陪", "孤独", "寂寞", "不被理解", "没人懂"],
    "成就": ["做到了", "成功", "进步", "考过", "写完", "学会"],
    "学业": ["上课", "作业", "老师", "考试", "专业", "学校", "影视"],
    "旅行": ["旅行", "出去玩", "机票", "酒店", "景点", "跳伞"],
    "音乐": ["听歌", "唱歌", "歌曲", "音乐", "旋律", "歌单", "R&B", "NewJeans", "Jpop"],
    "哲思": ["意义", "存在", "为什么", "人生", "本质", "意识"],
    "温暖": ["谢谢", "治愈", "安心", "感恩", "温暖", "幸福", "值得", "💕"],
}


class Tagger:
    """氛围标签打标器"""

    def __init__(self):
        pass

    async def tag(self, content: str) -> list[str]:
        """
        给一段内容打 3-5 个氛围标签
        返回: ["撒娇", "深夜", "猫猫"]
        """
        if not content.strip():
            return ["日常"]

        min_remote_chars = int(COST_OPTIMIZATION_CONFIG.get("remote_enrichment_min_chars", 120) or 120)
        if MEMORY_CONFIG.get("use_remote_enrichment", False) and len(content.strip()) >= min_remote_chars:
            try:
                tags = await self._api_tag(content)
                if tags:
                    return tags
            except Exception as e:
                print(f"[Tagger] API降级: {e}")

        return self._local_tag(content)

    async def _api_tag(self, content: str) -> list[str] | None:
        """Optional semantic tagging on the DeepSeek Flash mechanical route."""
        from api_cost import auxiliary_chat

        tags_str = "、".join(STANDARD_TAGS)
        prompt = f"""从以下 24 个标签中选 3-5 个最匹配的氛围标签。
只返回 JSON 数组，不要其他内容。

标签列表：{tags_str}

内容：
{content[:1500]}

返回格式：["标签1", "标签2", "标签3"]"""
        result = await auxiliary_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose="memory_tag",
            system_prompt="只返回JSON数组。",
            max_output_tokens=160,
        )
        if not result:
            return None
        text = str(result.get("content") or "").strip()
        text = re.sub(r"```json?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        tags = json.loads(text)
        return [t for t in tags if t in STANDARD_TAGS][:5] if isinstance(tags, list) else None

    def _local_tag(self, content: str) -> list[str]:
        """本地关键词降级"""
        text = content.lower()
        scores = {}

        for tag, keywords in TAG_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw in text)
            if hits > 0:
                scores[tag] = hits

        if not scores:
            return ["日常"]

        sorted_tags = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [t[0] for t in sorted_tags[:5]]

    async def tag_current_conversation(self, recent_messages: list[str]) -> list[str]:
        """给当前对话的最近几轮打标（用于检索时的标签匹配）"""
        combined = "\n".join(recent_messages[-6:])  # 最近3轮
        return await self.tag(combined)

    async def close(self):
        return None


# 全局单例
tagger = Tagger()
