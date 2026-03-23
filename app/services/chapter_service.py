import logging
import re

logger = logging.getLogger(__name__)


SPECIAL_TITLES = {
    "序章", "序", "楔子", "引子", "前言", "正文",
    "终章", "尾声", "后记", "番外", "番外篇", "附录", "完结感言"
}

STRONG_PATTERNS = [
    # 第15章 / 第 15 章 / 第十五章 / 第3卷 / 第十五回 / 第15章：归来
    r"^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册幕]\s*(?:[：:·\-—.．、]\s*.*)?$",
    r"^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册幕]\s+.*$",

    # chapter 1 / CHAPTER 1
    r"^chapter\s*\d+\s*(?:[:：.\-—]\s*.*)?$",
    r"^chapter\s*[ivxlcdm]+\s*(?:[:：.\-—]\s*.*)?$",
]

WEAK_PATTERNS = [
    # 1、标题 / 1. 标题 / 1．标题
    r"^\d+\s*[、.．\-—]\s*.+$",
    # 1 标题
    r"^\d+\s+.+$",
]


def _normalize_line(line: str) -> str:
    if line is None:
        return ""
    line = str(line).replace("\ufeff", "")
    line = line.replace("\u3000", " ")
    line = re.sub(r"[ \t]+", " ", line)
    return line.strip()


def _normalize_content(content: str) -> str:
    if not content:
        return ""
    return str(content).replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")


def _is_special_title(normalized: str) -> bool:
    return normalized in SPECIAL_TITLES


def _is_strong_title(normalized: str) -> bool:
    if not normalized:
        return False

    if _is_special_title(normalized):
        return True

    if len(normalized) > 80:
        return False

    for pattern in STRONG_PATTERNS:
        if re.match(pattern, normalized, re.IGNORECASE):
            return True
    return False


def _is_weak_title(normalized: str) -> bool:
    if not normalized:
        return False

    if len(normalized) > 60:
        return False

    for pattern in WEAK_PATTERNS:
        if re.match(pattern, normalized, re.IGNORECASE):
            return True
    return False


def _collect_following_text_length(lines: list, start_index: int, max_lookahead: int = 8) -> int:
    total_len = 0
    for i in range(start_index + 1, min(len(lines), start_index + 1 + max_lookahead)):
        text = _normalize_line(lines[i]["text"])
        if not text:
            continue
        if _is_strong_title(text):
            break
        total_len += len(text)
    return total_len


def _count_signals(lines: list):
    strong_count = 0
    weak_count = 0
    for item in lines:
        text = _normalize_line(item["text"])
        if not text:
            continue
        if _is_strong_title(text):
            strong_count += 1
        elif _is_weak_title(text):
            weak_count += 1
    return strong_count, weak_count


def _is_title_with_context(lines: list, index: int, strong_count: int, weak_count: int) -> bool:
    normalized = _normalize_line(lines[index]["text"])
    if not normalized:
        return False

    if _is_strong_title(normalized):
        return True

    if not _is_weak_title(normalized):
        return False

    following_len = _collect_following_text_length(lines, index, max_lookahead=8)
    if following_len >= 12:
        return True

    if weak_count >= 3:
        return True

    if re.match(r"^\d+\s*[、.．\-—]\s*.+$", normalized, re.IGNORECASE):
        return True

    if strong_count + weak_count >= 2 and re.match(r"^\d+\s+.+$", normalized, re.IGNORECASE):
        return True

    return False


class ChapterService:
    """
    最终版章节服务：
    - 统一后端章节解析
    - 返回 start_offset / end_offset
    - 可生成预览章节列表
    """

    def split_lines_with_offsets(self, content: str):
        content = _normalize_content(content)
        if not content:
            return []

        lines = []
        cursor = 0
        for raw_line in content.splitlines(keepends=True):
            line_start = cursor
            cursor += len(raw_line)
            line_text = raw_line.rstrip("\n").rstrip("\r")
            lines.append({
                "text": line_text,
                "start_offset": line_start,
                "end_offset": cursor,
            })

        # 如果文本末尾没有换行，splitlines(keepends=True) 也没问题
        # 这里不额外补空行
        return lines

    def parse_chapter_bounds(self, content: str):
        """
        返回章节边界：
        [
            {
                "index": 1,
                "title": "...",
                "start_offset": 123,
                "end_offset": 456,
            }
        ]
        """
        content = _normalize_content(content)
        if not content.strip():
            return []

        lines = self.split_lines_with_offsets(content)
        if not lines:
            return []

        strong_count, weak_count = _count_signals(lines)
        title_positions = []

        for idx, item in enumerate(lines):
            normalized = _normalize_line(item["text"])
            if not normalized:
                continue

            if _is_title_with_context(lines, idx, strong_count, weak_count):
                title_positions.append({
                    "line_index": idx,
                    "title": normalized,
                    "start_offset": item["start_offset"],
                })

        if not title_positions:
            return []

        chapters = []
        for i, item in enumerate(title_positions):
            start_offset = item["start_offset"]
            if i + 1 < len(title_positions):
                end_offset = title_positions[i + 1]["start_offset"]
            else:
                end_offset = len(content)

            chapters.append({
                "index": i + 1,
                "title": item["title"],
                "start_offset": start_offset,
                "end_offset": end_offset,
            })

        logger.info(f"章节解析完成: 共识别 {len(chapters)} 章")
        return chapters

    def build_preview_chapters(self, content: str):
        """
        返回前端预览所需的轻量结构
        """
        bounds = self.parse_chapter_bounds(content)
        return [
            {
                "index": item["index"],
                "title": item["title"],
            }
            for item in bounds
        ]

    def slice_chapter_range(self, chapter_bounds: list, start_chapter: int = None, end_chapter: int = None):
        total = len(chapter_bounds)
        if total == 0:
            return [], 0, 0

        try:
            start = int(start_chapter) if start_chapter is not None else 1
        except (TypeError, ValueError):
            start = 1

        try:
            end = int(end_chapter) if end_chapter is not None else total
        except (TypeError, ValueError):
            end = total

        start = max(1, min(start, total))
        end = max(1, min(end, total))

        if start > end:
            start, end = end, start

        selected = chapter_bounds[start - 1:end]
        return selected, start, end

    def get_chapter_text(self, content: str, chapter: dict):
        if not content or not chapter:
            return ""
        start_offset = max(0, int(chapter.get("start_offset", 0)))
        end_offset = min(len(content), int(chapter.get("end_offset", len(content))))
        if end_offset < start_offset:
            end_offset = start_offset
        return content[start_offset:end_offset]

    def build_chapter_objects(self, content: str):
        """
        如需完整章文本对象，可用这个方法：
        [
            {
                "index": 1,
                "title": "...",
                "start_offset": ...,
                "end_offset": ...,
                "content": "该章原文"
            }
        ]
        """
        bounds = self.parse_chapter_bounds(content)
        result = []
        for item in bounds:
            obj = dict(item)
            obj["content"] = self.get_chapter_text(content, item)
            result.append(obj)
        return result


chapter_service = ChapterService()