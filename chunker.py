"""
话题切分器 (Chunker)
按话题拆成短片段，每条聚焦一个事件/话题
解决：一段话混着猫+噩梦+小说，embedding 取平均语义，搜什么都半相关

两种模式：
1. API 模式：可选用 DeepSeek Flash 辅助通道做语义切分
2. 本地降级：按换行/标点/长度规则切
"""
import json
import re
from config import MEMORY_CONFIG


# 切分参数
MIN_CHUNK_LENGTH = 20      # 太短的不要
MAX_CHUNK_LENGTH = 500     # 太长的再切
OVERLAP_CHARS = 30         # 前后片段重叠字数（保持上下文连贯）


class Chunker:
    """话题切分器"""

    def __init__(self):
        pass

    async def chunk(self, content: str, source: str = "chat") -> list[dict]:
        """
        把一段内容切成多个话题片段
        返回: [{"text": "...", "topic_hint": "猫猫", "index": 0}, ...]
        """
        content = content.strip()
        if not content:
            return []

        # 短内容不用切
        if len(content) <= MAX_CHUNK_LENGTH:
            return [{"text": content, "topic_hint": "", "index": 0}]

        # 远程语义切分默认关闭：它会在每条长消息上额外消耗一次 API。
        if MEMORY_CONFIG.get("use_remote_enrichment", False):
            try:
                chunks = await self._api_chunk(content)
                if chunks and len(chunks) > 0:
                    return chunks
            except Exception as e:
                print(f"[Chunker] API降级: {e}")

        # 本地降级
        return self._local_chunk(content)

    async def _api_chunk(self, content: str) -> list[dict]:
        """Optional semantic chunking on the DeepSeek Flash mechanical route."""
        from api_cost import auxiliary_chat

        prompt = f"""把以下对话内容按话题切分成独立片段。每个片段聚焦一个话题/事件。

规则：
1. 每个片段 20-500 字
2. 话题变了就切
3. 给每个片段一个简短的话题提示词（2-4个字）
4. 只返回 JSON 数组，不要其他内容

返回格式：
[{{"text": "片段内容", "topic_hint": "话题提示"}}]

内容：
{content[:3000]}"""
        result = await auxiliary_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose="memory_chunk",
            system_prompt="只返回JSON数组。",
            max_output_tokens=1600,
        )
        if not result:
            return []
        text = str(result.get("content") or "").strip()
        text = re.sub(r"```json?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        raw = json.loads(text)
        chunks = []
        for i, item in enumerate(raw if isinstance(raw, list) else []):
            t = str(item.get("text") or "").strip() if isinstance(item, dict) else ""
            if len(t) >= MIN_CHUNK_LENGTH:
                chunks.append({
                    "text": t,
                    "topic_hint": str(item.get("topic_hint") or "")[:20],
                    "index": i,
                })
        return chunks

    def _local_chunk(self, content: str) -> list[dict]:
        """本地降级切分：按段落+句号+长度"""
        # 先按段落切
        paragraphs = re.split(r'\n{2,}', content)

        raw_chunks = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(para) <= MAX_CHUNK_LENGTH:
                raw_chunks.append(para)
            else:
                # 长段落按句号切
                sentences = re.split(r'(?<=[。！？\.\!\?])\s*', para)
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) > MAX_CHUNK_LENGTH:
                        if current:
                            raw_chunks.append(current)
                        current = sent
                    else:
                        current += sent
                if current:
                    raw_chunks.append(current)

        # 过滤太短的 + 编号
        chunks = []
        for i, text in enumerate(raw_chunks):
            if len(text) >= MIN_CHUNK_LENGTH:
                chunks.append({
                    "text": text,
                    "topic_hint": "",  # 本地模式没有话题提示
                    "index": i,
                })

        return chunks if chunks else [{"text": content[:MAX_CHUNK_LENGTH], "topic_hint": "", "index": 0}]

    async def close(self):
        return None


# 全局单例
chunker = Chunker()
