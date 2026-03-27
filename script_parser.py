from __future__ import annotations

import re
from dataclasses import dataclass


BODY_MARKER_PATTERN = re.compile(r"(?m)^\s*剧本正文\s*[：:]?\s*$")
SECTION_MARKER_PATTERN = re.compile(
    r"(?m)^\s*(故事梗概|人物小传|核心场景|剧本正文)\s*[：:]?\s*$"
)
NON_WHITESPACE_PATTERN = re.compile(r"\s+", re.MULTILINE)
ACT_HEADER_PATTERN = re.compile(
    r"(?m)^\s*(?:第(?:[0-9０-９]+|[一二三四五六七八九十百千万零两]+)\s*幕|序幕|终幕|尾声)(?:\s*[：:]\s*.*)?\s*$"
)
EPISODE_HEADER_PATTERN = re.compile(
    r"(?m)^(?P<header>\s*第(?:[0-9０-９]+|[一二三四五六七八九十百千万零两]+)\s*集(?:\s*[：:]\s*.*)?\s*)$"
)
SCENE_HEADER_PATTERN = re.compile(
    r"(?m)^(?P<header>\s*(?:"
    r"【\s*(?:第[一二三四五六七八九十百千万零两\d]+场|场景[一二三四五六七八九十百千万零两\d]+)[^】]*】"
    r"|第[一二三四五六七八九十百千万零两\d]+场(?:\s*[：:]\s*.*)?"
    r"|场景[一二三四五六七八九十百千万零两\d]+(?:\s*[：:]\s*.*)?"
    r"|SCENE\s*\d+(?:\s*[：:]\s*.*)?"
    r"))\s*$"
)
SCENE_FIELD_LINE_PATTERN = re.compile(
    r"^\s*(?:时间|地点|人物|出场人物|角色|场景|场次|内景|外景|环境|天气|氛围|INT\.|EXT\.)\s*[：:].*$",
    re.IGNORECASE,
)
TRANSITION_LINE_PATTERN = re.compile(
    r"^\s*(?:转场|镜头切换|切到|CUT TO|FADE IN|FADE OUT)\b.*$",
    re.IGNORECASE,
)
FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*]+')
MIN_SCENE_VISIBLE_CHARS = 28
PREFERRED_FALLBACK_SCENE_CHARS = 90


@dataclass(frozen=True)
class EpisodeBlock:
    heading: str
    content: str


@dataclass(frozen=True)
class SceneSplit:
    prelude: str
    first_scene: str
    remainder: str


@dataclass(frozen=True)
class ParsedScript:
    title: str
    script_prefix: str
    body_prefix: str
    episodes: list[EpisodeBlock]
    sections: dict[str, str]

    @property
    def download_name(self) -> str:
        base_name = self.title.strip().rstrip("、，,")
        safe_name = FILENAME_INVALID_PATTERN.sub("-", base_name).strip().strip(".")
        if not safe_name:
            safe_name = "改写后剧本"
        return f"{safe_name}.txt"


def decode_text_file(raw: bytes) -> str:
    encodings = ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "gb18030", "gbk", "big5")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_script(text: str) -> ParsedScript:
    normalized_text = text.replace("\r\n", "\n")
    title = extract_title(normalized_text)
    script_prefix, body_text = split_body(normalized_text)
    body_prefix, episodes = split_into_episodes(body_text)
    if not episodes:
        raise ValueError("没有识别到任何“第X集”标题，请检查剧本正文格式。")
    sections = extract_sections(script_prefix)
    return ParsedScript(
        title=title,
        script_prefix=script_prefix,
        body_prefix=body_prefix,
        episodes=episodes,
        sections=sections,
    )


def extract_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "完整剧本"


def split_body(text: str) -> tuple[str, str]:
    match = BODY_MARKER_PATTERN.search(text)
    if not match:
        raise ValueError("没有识别到“剧本正文”字段，请检查输入格式。")
    return text[: match.end()], text[match.end() :]


def extract_sections(prefix_text: str) -> dict[str, str]:
    matches = list(SECTION_MARKER_PATTERN.finditer(prefix_text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prefix_text)
        sections[match.group(1)] = prefix_text[start:end].strip()
    return sections


def split_into_episodes(body_text: str) -> tuple[str, list[EpisodeBlock]]:
    matches = list(EPISODE_HEADER_PATTERN.finditer(body_text))
    if not matches:
        return body_text, []
    body_prefix = clean_body_prefix(body_text[: matches[0].start()])
    episodes: list[EpisodeBlock] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body_text)
        episodes.append(
            EpisodeBlock(
                heading=match.group("header"),
                content=clean_episode_content(body_text[start:end]),
            )
        )
    return body_prefix, episodes


def clean_body_prefix(body_prefix: str) -> str:
    cleaned = ACT_HEADER_PATTERN.sub("", body_prefix)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if cleaned.strip():
        return cleaned
    return "\n" if body_prefix else ""


def clean_episode_content(content: str) -> str:
    cleaned = ACT_HEADER_PATTERN.sub("", content)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def split_first_scene(content: str) -> SceneSplit:
    explicit_split = split_by_explicit_scene_headers(content)
    structured_split = split_by_structured_fields(content)
    fallback_split = split_first_scene_fallback(content)

    if explicit_split and not is_scene_suspicious(explicit_split.first_scene):
        return explicit_split

    for candidate in (structured_split, fallback_split):
        if candidate and not is_scene_suspicious(candidate.first_scene):
            return candidate

    best_split = choose_best_scene_split([explicit_split, structured_split, fallback_split])
    if best_split:
        return best_split
    return SceneSplit(prelude="", first_scene=content, remainder="")


def split_by_explicit_scene_headers(content: str) -> SceneSplit | None:
    matches = list(SCENE_HEADER_PATTERN.finditer(content))
    if not matches:
        return None
    first = matches[0]
    prelude = content[: first.start()]
    for index in range(1, len(matches) + 1):
        second = matches[index] if index < len(matches) else None
        split_at = second.start() if second else len(content)
        candidate_scene = content[first.start() : split_at]
        remainder = content[split_at:] if second else ""
        candidate = SceneSplit(prelude=prelude, first_scene=candidate_scene, remainder=remainder)
        if not is_scene_suspicious(candidate_scene) or second is None:
            return candidate
    return None


def split_by_structured_fields(content: str) -> SceneSplit | None:
    stripped = content.lstrip("\n")
    leading = content[: len(content) - len(stripped)]
    if not stripped:
        return None

    lines = stripped.splitlines(keepends=True)
    if not lines:
        return None

    positions: list[int] = []
    offset = 0
    blank_run = 0
    for index, line in enumerate(lines):
        stripped_line = line.strip()
        if stripped_line:
            if blank_run >= 1 and looks_like_scene_start_block(lines, index):
                positions.append(offset)
            blank_run = 0
        else:
            blank_run += 1
        offset += len(line)

    for position in positions:
        candidate_scene = stripped[:position]
        remainder = stripped[position:]
        if not candidate_scene.strip():
            continue
        if scene_visible_chars(candidate_scene) >= MIN_SCENE_VISIBLE_CHARS:
            return SceneSplit(prelude=leading, first_scene=candidate_scene, remainder=remainder)
    return None


def split_first_scene_fallback(content: str) -> SceneSplit:
    stripped = content.lstrip("\n")
    leading = content[: len(content) - len(stripped)]
    if not stripped:
        return SceneSplit(prelude=content, first_scene="", remainder="")
    paragraph_starts = [0]
    paragraph_starts.extend(match.end() for match in re.finditer(r"\n{2,}", stripped))
    paragraph_starts = [start for start in paragraph_starts if start < len(stripped)]

    chosen_split_at: int | None = None
    for index in range(1, len(paragraph_starts)):
        split_at = paragraph_starts[index]
        candidate_scene = stripped[:split_at]
        next_block = stripped[split_at:]
        if scene_visible_chars(candidate_scene) >= PREFERRED_FALLBACK_SCENE_CHARS:
            chosen_split_at = split_at
            break
        if scene_visible_chars(candidate_scene) >= MIN_SCENE_VISIBLE_CHARS and looks_like_scene_start_text(next_block):
            chosen_split_at = split_at
            break

    if chosen_split_at is None:
        return SceneSplit(prelude=leading, first_scene=stripped, remainder="")
    return SceneSplit(
        prelude=leading,
        first_scene=stripped[:chosen_split_at],
        remainder=stripped[chosen_split_at:],
    )


def choose_best_scene_split(candidates: list[SceneSplit | None]) -> SceneSplit | None:
    valid_candidates = [candidate for candidate in candidates if candidate and candidate.first_scene.strip()]
    if not valid_candidates:
        return None
    non_suspicious = [candidate for candidate in valid_candidates if not is_scene_suspicious(candidate.first_scene)]
    if non_suspicious:
        return min(non_suspicious, key=lambda item: scene_quality(item.first_scene))
    return max(valid_candidates, key=lambda item: scene_quality(item.first_scene))


def scene_quality(scene_text: str) -> tuple[int, int]:
    return scene_visible_chars(scene_text), substantive_line_count(scene_text)


def is_scene_suspicious(scene_text: str) -> bool:
    visible_chars = scene_visible_chars(scene_text)
    content_lines = substantive_line_count(scene_text)
    return visible_chars < MIN_SCENE_VISIBLE_CHARS and content_lines <= 1


def scene_visible_chars(text: str) -> int:
    return len(NON_WHITESPACE_PATTERN.sub("", text))


def substantive_line_count(scene_text: str) -> int:
    count = 0
    for line in scene_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if SCENE_HEADER_PATTERN.match(stripped):
            continue
        if SCENE_FIELD_LINE_PATTERN.match(stripped):
            continue
        if TRANSITION_LINE_PATTERN.match(stripped):
            continue
        count += 1
    return count


def looks_like_scene_start_block(lines: list[str], start_index: int) -> bool:
    if start_index >= len(lines):
        return False
    first_line = lines[start_index].strip()
    if not first_line:
        return False
    if SCENE_HEADER_PATTERN.match(first_line) or TRANSITION_LINE_PATTERN.match(first_line):
        return True
    field_hits = 0
    for line in lines[start_index : start_index + 4]:
        stripped = line.strip()
        if not stripped:
            break
        if SCENE_FIELD_LINE_PATTERN.match(stripped):
            field_hits += 1
        elif field_hits:
            break
    return field_hits >= 2


def looks_like_scene_start_text(text: str) -> bool:
    lines = text.splitlines()
    for index, line in enumerate(lines[:4]):
        stripped = line.strip()
        if not stripped:
            continue
        if SCENE_HEADER_PATTERN.match(stripped) or TRANSITION_LINE_PATTERN.match(stripped):
            return True
        return looks_like_scene_start_block(lines, index)
    return False


def clean_model_output(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = re.sub(r"^(改写后的?第?一?个?场景[:：]\s*)", "", cleaned)
    cleaned = re.sub(r"^(以下是改写[:：]\s*)", "", cleaned)
    return cleaned.strip()


def preserve_scene_whitespace(original_scene: str, rewritten_scene: str) -> str:
    leading_match = re.match(r"^\s*", original_scene)
    trailing_match = re.search(r"\s*$", original_scene)
    leading = leading_match.group(0) if leading_match else ""
    trailing = trailing_match.group(0) if trailing_match else ""
    return f"{leading}{rewritten_scene.strip()}{trailing}"


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def restore_scene_heading(original_scene: str, rewritten_scene: str) -> str:
    original_first_line = first_non_empty_line(original_scene)
    rewritten_first_line = first_non_empty_line(rewritten_scene)
    if (
        original_first_line
        and SCENE_HEADER_PATTERN.match(original_first_line)
        and original_first_line != rewritten_first_line
    ):
        return f"{original_first_line}\n{rewritten_scene.lstrip()}"
    return rewritten_scene
