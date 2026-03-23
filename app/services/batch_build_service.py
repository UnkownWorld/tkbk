import logging

from app.services.chapter_service import chapter_service

logger = logging.getLogger(__name__)


def _safe_int(value, default, min_value=None, max_value=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


class BatchBuildService:
    """
    最终版批次构建服务：
    - 基于原始全文 + 后端章节边界构建批次
    - 每批次内容 = 原始全文切片
    """

    def parse_and_select_chapters(self, content: str, start_chapter: int = None, end_chapter: int = None):
        chapter_bounds = chapter_service.parse_chapter_bounds(content)
        selected, actual_start, actual_end = chapter_service.slice_chapter_range(
            chapter_bounds=chapter_bounds,
            start_chapter=start_chapter,
            end_chapter=end_chapter
        )
        return chapter_bounds, selected, actual_start, actual_end

    def build_batches(self, content: str, chapter_bounds: list, batch_size: int):
        """
        输入：
        - content: 原始全文
        - chapter_bounds: 已选章节边界列表（必须带 index/title/start_offset/end_offset）
        - batch_size: 每批多少章

        输出：
        [
            {
                "batch_index": 1,
                "chapter_start": 1,
                "chapter_end": 10,
                "chapter_count": 10,
                "chapter_titles": [...],
                "start_offset": 123,
                "end_offset": 4567,
                "content": "原始全文切片"
            }
        ]
        """
        content = content or ""
        chapter_bounds = chapter_bounds or []
        batch_size = _safe_int(batch_size, 10, min_value=1)

        if not content or not chapter_bounds:
            return []

        batches = []
        total = len(chapter_bounds)

        for i in range(0, total, batch_size):
            group = chapter_bounds[i:i + batch_size]
            if not group:
                continue

            start_offset = int(group[0]["start_offset"])

            # 注意：这里 end_offset 取“下一批第一章的 start_offset”不是必须，
            # 因为 group[-1] 自身 end_offset 已经是“下一章前”。
            # 直接取最后一章 end_offset 更自然，也不会错位。
            end_offset = int(group[-1]["end_offset"])

            if end_offset < start_offset:
                end_offset = start_offset

            batch_content = content[start_offset:end_offset]

            batches.append({
                "batch_index": len(batches) + 1,
                "chapter_start": int(group[0]["index"]),
                "chapter_end": int(group[-1]["index"]),
                "chapter_count": len(group),
                "chapter_titles": [item["title"] for item in group],
                "start_offset": start_offset,
                "end_offset": end_offset,
                "content": batch_content,
            })

        logger.info(f"批次构建完成: 共 {len(batches)} 批")
        return batches

    def build_batches_from_content(self, content: str, batch_size: int, start_chapter: int = None, end_chapter: int = None):
        """
        一步完成：
        1. 解析全书章节
        2. 按章节范围筛选
        3. 按 batch_size 构建批次
        """
        all_bounds, selected_bounds, actual_start, actual_end = self.parse_and_select_chapters(
            content=content,
            start_chapter=start_chapter,
            end_chapter=end_chapter
        )

        batches = self.build_batches(
            content=content,
            chapter_bounds=selected_bounds,
            batch_size=batch_size
        )

        return {
            "all_chapters": all_bounds,
            "selected_chapters": selected_bounds,
            "actual_start": actual_start,
            "actual_end": actual_end,
            "batches": batches,
        }

    def build_book_execution_plan(self, file_name: str, content: str, config_id: str, batch_size: int, start_chapter: int = None, end_chapter: int = None):
        """
        为一本书生成完整执行计划
        """
        built = self.build_batches_from_content(
            content=content,
            batch_size=batch_size,
            start_chapter=start_chapter,
            end_chapter=end_chapter
        )

        return {
            "file_name": file_name or "未命名",
            "content": content or "",
            "config_id": config_id or "",
            "batch_size": _safe_int(batch_size, 10, min_value=1),
            "start_chapter": built["actual_start"],
            "end_chapter": built["actual_end"],
            "all_chapters": built["all_chapters"],
            "selected_chapters": built["selected_chapters"],
            "total_chapters": len(built["selected_chapters"]),
            "batches": built["batches"],
        }


batch_build_service = BatchBuildService()