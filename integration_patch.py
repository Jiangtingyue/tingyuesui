"""
integration_patch.py
把 Ombre Brain 的情感记忆整合进大西瓜聊天流程

用法：在 app.py 里 import 这个模块，调用 apply_patch(app) 注册路由
或者直接把这些代码合并进 app.py
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dehydrator import dehydrator
from decay_engine import (
    run_decay_cycle, activate_memory, resolve_memory,
    get_surfacing_memories, calc_decay_score
)
from vector_search_v2 import vector_search_v2


def apply_patch(app: FastAPI, scheduler=None):
    """注册所有新路由和定时任务"""

    # ── 定时任务：每小时跑一次遗忘衰减 ──
    if scheduler:
        scheduler.add_job(
            run_decay_cycle, "interval", hours=1, id="decay_cycle"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  记忆 API（升级版）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @app.post("/api/memory/process")
    async def memory_process(request: Request):
        """
        完整记忆处理流程：脱水压缩 → 情感打标 → 向量索引
        前端或聊天流程调用
        """
        body = await request.json()
        content = body.get("content", "")
        source = body.get("source", "chat")

        if not content.strip():
            return JSONResponse({"error": "内容为空"}, status_code=400)

        # 1. 脱水压缩 + 情感打标
        emotion_data = await dehydrator.process(content, source)

        # 2. 向量索引（带情感数据）
        mem_id = await vector_search_v2.index_message(
            content=content,
            source=source,
            emotion_data=emotion_data,
        )

        return JSONResponse({
            "ok": True,
            "memory_id": mem_id,
            "emotion": {
                "valence": emotion_data.get("valence"),
                "arousal": emotion_data.get("arousal"),
                "domain": emotion_data.get("domain"),
                "importance": emotion_data.get("importance"),
            },
            "summary": emotion_data.get("summary", ""),
        })

    @app.get("/api/memory/surface")
    async def memory_surface(limit: int = 5):
        """主动浮现：返回当前权重最高的未解决记忆"""
        memories = get_surfacing_memories(limit)
        return JSONResponse(memories)

    @app.post("/api/memory/search-v2")
    async def memory_search_v2(request: Request):
        """语义搜索（带情感加权）"""
        body = await request.json()
        query = body.get("query", "")
        results = vector_search_v2.search(query)
        return JSONResponse(results)

    @app.post("/api/memory/resolve")
    async def memory_resolve(request: Request):
        """标记记忆为已解决"""
        body = await request.json()
        memory_id = body.get("memory_id")
        if memory_id:
            resolve_memory(memory_id)
        return JSONResponse({"ok": True})

    @app.post("/api/memory/decay-cycle")
    async def trigger_decay(request: Request):
        """手动触发一次衰减周期"""
        result = run_decay_cycle()
        return JSONResponse(result)

    @app.get("/api/memory/emotion-map")
    async def emotion_map():
        """返回所有记忆的情感坐标（给前端画散点图用）"""
        from models import get_db
        with get_db() as db:
            rows = db.execute(
                """SELECT id, content, valence, arousal, 
                          decay_score, domain, importance, resolved
                   FROM memories 
                   WHERE archived = 0 AND valence IS NOT NULL
                   ORDER BY decay_score DESC
                   LIMIT 200"""
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  聊天流程接入指南
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
#  在 app.py 的 /api/chat 接口里做这些改动：
#
#  1. 替换 memory_context 构建：
#     旧：memory_context = vector_search.build_memory_context(message)
#     新：memory_context = vector_search_v2.build_memory_context(message)
#          ↑ 这会自动包含主动浮现 + 语义搜索
#
#  2. 流结束后，异步处理记忆：
#     旧：vector_search.index_message(message, source="chat")
#     新：emotion_data = await dehydrator.process(message)
#         await vector_search_v2.index_message(message, "chat", emotion_data)
#         if len(full_response) > 50:
#             resp_emotion = await dehydrator.process(full_response)
#             await vector_search_v2.index_message(
#                 full_response, "chat_response", resp_emotion
#             )
#
#  3. 在 lifespan 里加衰减定时任务：
#     scheduler.add_job(run_decay_cycle, "interval", hours=1, id="decay")
#
#  完整改动参考上面的代码，核心就是：
#  vector_search → vector_search_v2
#  + 加 dehydrator 处理
#  + 加 decay 定时任务
