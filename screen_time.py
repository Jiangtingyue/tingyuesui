"""
屏幕时间采集
iOS 快捷指令开关 app 时自动发 HTTP 请求
toggle 逻辑计算使用时长
AI 发消息前读这些数据
"""
from datetime import datetime, timedelta

from models import get_db, save_screen_event, get_recent_screen_time


class ScreenTimeTracker:
    """屏幕时间追踪器"""

    def __init__(self):
        self._active_sessions = {}  # app_name -> open_time

    def handle_event(self, event_type: str, app_name: str = "phone") -> dict:
        """
        处理 iOS 快捷指令发来的事件
        event_type: app_open / app_close
        
        iOS 快捷指令设置：
        - 打开App时: GET https://your-vps/api/screen/open?app=AppName
        - 关闭App时: GET https://your-vps/api/screen/close?app=AppName
        """
        now = datetime.now()

        if event_type == "app_open":
            self._active_sessions[app_name] = now
            save_screen_event("app_open", app_name)
            return {"status": "ok", "event": "open", "app": app_name}

        elif event_type == "app_close":
            duration = 0
            if app_name in self._active_sessions:
                open_time = self._active_sessions.pop(app_name)
                duration = int((now - open_time).total_seconds())

            save_screen_event("app_close", app_name, duration)
            return {
                "status": "ok",
                "event": "close",
                "app": app_name,
                "duration_seconds": duration,
                "duration_display": self._format_duration(duration),
            }

        # toggle 模式（同一个快捷指令切换）
        elif event_type == "toggle":
            if app_name in self._active_sessions:
                return self.handle_event("app_close", app_name)
            else:
                return self.handle_event("app_open", app_name)

        return {"status": "error", "message": f"未知事件类型: {event_type}"}

    def get_summary(self, hours: int = 24) -> dict:
        """
        获取屏幕时间摘要
        AI 发消息前调用
        """
        events = get_recent_screen_time(hours)

        if not events:
            return {
                "total_minutes": 0,
                "sessions": 0,
                "apps": {},
                "current_active": list(self._active_sessions.keys()),
            }

        # 按 app 聚合使用时长
        app_durations = {}
        for e in events:
            app = e.get("app_name", "unknown")
            dur = e.get("duration_seconds", 0)
            if app not in app_durations:
                app_durations[app] = {"total_seconds": 0, "sessions": 0}
            app_durations[app]["total_seconds"] += dur
            if e.get("event_type") == "app_close":
                app_durations[app]["sessions"] += 1

        total_seconds = sum(d["total_seconds"] for d in app_durations.values())

        return {
            "total_minutes": round(total_seconds / 60, 1),
            "total_display": self._format_duration(total_seconds),
            "sessions": sum(d["sessions"] for d in app_durations.values()),
            "apps": {
                app: {
                    "minutes": round(d["total_seconds"] / 60, 1),
                    "sessions": d["sessions"],
                }
                for app, d in sorted(
                    app_durations.items(),
                    key=lambda x: x[1]["total_seconds"],
                    reverse=True
                )
            },
            "current_active": list(self._active_sessions.keys()),
            "period_hours": hours,
        }

    def build_context_for_ai(self) -> str:
        """
        构建给 AI 看的屏幕时间上下文
        注入到关心系统
        """
        summary = self.get_summary(hours=6)

        if summary["total_minutes"] < 5:
            return ""

        lines = [f"最近6小时屏幕使用: {summary['total_display']}"]

        if summary["apps"]:
            top_apps = list(summary["apps"].items())[:3]
            apps_str = ", ".join(
                f"{app}({info['minutes']:.0f}分钟)"
                for app, info in top_apps
            )
            lines.append(f"主要使用: {apps_str}")

        if summary["current_active"]:
            lines.append(f"当前正在使用: {', '.join(summary['current_active'])}")

        return "\n".join(lines)

    def _format_duration(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}秒"
        elif seconds < 3600:
            return f"{seconds // 60}分钟"
        else:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h}小时{m}分钟"


# 全局单例
screen_tracker = ScreenTimeTracker()
