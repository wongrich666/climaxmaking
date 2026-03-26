from __future__ import annotations

from dataclasses import dataclass

from llm_client import LLMClient
from script_parser import (
    ParsedScript,
    clean_model_output,
    parse_script,
    preserve_scene_whitespace,
    restore_scene_heading,
    split_first_scene,
)


SYSTEM_PROMPT = """
你的任务只有一个：只改写单集的第一个场景，让观众在开头 3 秒内被抓住。

严格遵守以下规则：

1. 只能改写“第一个场景”，不得改写第二场及后续内容；
2. 不得改变本集已有剧情事实、人物关系、时间线、地点、信息顺序或任何情节走向；
3. 不得新增任何未在原场景或本集剧情中已存在的事件、结果或角色行动；所有冲突/爆点必须来自原场景已有线索；
4. 必须融入“黄金3秒法则 / Golden Opening”：开头 1–3 句必须立即抛出强冲突、高危机、大异常、羞辱、压迫感、爆炸性张力或剧烈对峙；
5. 强化冲击但保持合理兼容本集后续：可以强化耳光、事故、突发对峙、羞辱揭露、惊险失控等元素，但不得扭曲剧情主线；
6. 保留原有结构化格式与字段（如场次标题、时间、地点、人物标识等），仅在原框架内增强表现力；
7. 语言要更狠、更抓人、更有张力，允许更尖锐、更具压迫/爆炸感，但不得脱离原剧情成为另一个故事；
8. 不要解释，不要分析，不要加入引号或代码块，只输出改写后的“第一个场景”全文。
9. 字数的浮动只能在原文的10%以内

现在请开始改写。
"""


@dataclass(frozen=True)
class RewriteResult:
    title: str
    content: str
    download_name: str
    episode_count: int
    provider: str


class ScriptRewriter:
    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def rewrite_script(self, script_text: str, provider_name: str) -> RewriteResult:
        parsed = parse_script(script_text)
        rewritten_episodes = [
            self._rewrite_episode(parsed, episode.heading, episode.content, provider_name)
            for episode in parsed.episodes
        ]
        rebuilt_script = (
            f"{parsed.script_prefix}{parsed.body_prefix}{''.join(rewritten_episodes)}"
        )
        return RewriteResult(
            title=parsed.title,
            content=rebuilt_script,
            download_name=parsed.download_name,
            episode_count=len(parsed.episodes),
            provider=provider_name,
        )

    def _rewrite_episode(
        self,
        parsed: ParsedScript,
        episode_heading: str,
        episode_content: str,
        provider_name: str,
    ) -> str:
        scene_split = split_first_scene(episode_content)
        if not scene_split.first_scene.strip():
            return f"{episode_heading}{episode_content}"

        user_prompt = self._build_user_prompt(
            parsed=parsed,
            episode_heading=episode_heading.strip(),
            first_scene=scene_split.first_scene.strip(),
            full_episode=f"{scene_split.prelude}{scene_split.first_scene}{scene_split.remainder}".strip(),
            next_content=scene_split.remainder.strip(),
        )
        rewritten_scene = self.llm_client.chat(
            provider_name=provider_name,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        cleaned = clean_model_output(rewritten_scene)
        restored = restore_scene_heading(scene_split.first_scene, cleaned)
        final_scene = preserve_scene_whitespace(scene_split.first_scene, restored)
        return f"{episode_heading}{scene_split.prelude}{final_scene}{scene_split.remainder}"

    @staticmethod
    def _build_user_prompt(
        parsed: ParsedScript,
        episode_heading: str,
        first_scene: str,
        full_episode: str,
        next_content: str,
    ) -> str:
        story_summary = parsed.sections.get("故事梗概", "")
        characters = parsed.sections.get("人物小传", "")
        core_scenes = parsed.sections.get("核心场景", "")
        tail_excerpt = next_content[:4000] if next_content else "无"
        full_episode_excerpt = full_episode[:9000]
        return f"""请改写下面这集的第一个场景。

【整部剧标题】
{parsed.title}

【故事梗概】
{story_summary or "无"}

【人物小传】
{characters or "无"}

【核心场景】
{core_scenes or "无"}

【当前集标题】
{episode_heading}

【本集全文参考】
{full_episode_excerpt}

【后续剧情衔接参考】
{tail_excerpt}

【需要改写的第一个场景原文】
{first_scene}

改写目标：
- 开头第一眼就抓住观众，做到“开篇即爆”。
- 把最强冲突、羞辱、危险、压迫或反转尽量前置。
- 爽点和危机要更明确，语言更有张力，允许轻微疯感。
- 但绝不能偏离本集原有剧情，不要改后续发生的事实。
- 保留场景结构化字段和格式习惯。

现在直接输出改写后的第一个场景全文。"""
