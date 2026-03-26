from __future__ import annotations

import re
from dataclasses import dataclass


BODY_MARKER_PATTERN = re.compile(r"(?m)^·?\s*剧本正文\s*[：:]?\s*$")
SECTION_MARKER_PATTERN = re.compile(
    r"(?m)^·\s*(故事梗概|人物小传|核心场景|剧本正文)\s*[：:]?\s*$"
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
FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*]+')


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
        raise ValueError("没有识别到“·剧本正文”字段，请检查输入格式。")
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
    body_prefix = body_text[: matches[0].start()]
    episodes: list[EpisodeBlock] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body_text)
        episodes.append(EpisodeBlock(heading=match.group("header"), content=body_text[start:end]))
    return body_prefix, episodes


def split_first_scene(content: str) -> SceneSplit:
    matches = list(SCENE_HEADER_PATTERN.finditer(content))
    if matches:
        first = matches[0]
        second = matches[1] if len(matches) > 1 else None
        prelude = content[: first.start()]
        first_scene = content[first.start() : second.start() if second else len(content)]
        remainder = content[second.start() :] if second else ""
        return SceneSplit(prelude=prelude, first_scene=first_scene, remainder=remainder)
    return split_first_scene_fallback(content)


def split_first_scene_fallback(content: str) -> SceneSplit:
    stripped = content.lstrip("\n")
    leading = content[: len(content) - len(stripped)]
    if not stripped:
        return SceneSplit(prelude=content, first_scene="", remainder="")
    breaks = list(re.finditer(r"\n{2,}", stripped))
    if len(breaks) >= 2:
        split_at = breaks[1].start()
        return SceneSplit(
            prelude=leading,
            first_scene=stripped[:split_at],
            remainder=stripped[split_at:],
        )
    return SceneSplit(prelude=leading, first_scene=stripped, remainder="")


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
