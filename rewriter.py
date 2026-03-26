from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Callable

from llm_client import LLMClient
from script_parser import (
    EpisodeBlock,
    ParsedScript,
    clean_model_output,
    parse_script,
    preserve_scene_whitespace,
    restore_scene_heading,
    split_first_scene,
)


logger = logging.getLogger("golden_opening.rewriter")
NON_WHITESPACE_PATTERN = re.compile(r"\s+", re.MULTILINE)
JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
MAX_REWRITE_ATTEMPTS = 5

SYSTEM_PROMPT = """你是一个擅长短剧“开篇即爆”强化的专业编剧编辑。

你的任务只有一个：只改写单集的第一个场景，让观众在开头 3 秒内被抓住。

必须严格遵守：
1. 只能改写“第一个场景”，不能改写第二场及后续内容。
2. 不能改变本集已有剧情事实、人物关系、时间线、地点、结果和信息顺序的大方向。
3. 必须融入“黄金3秒法则 / Golden Opening”：开头 1-3 句就切入高潮、强冲突、重大异常、羞辱、压迫、危机或反转。
4. 可以强化耳光、事故、捉奸、下跪、处刑、爆炸、猎奇新闻、极端对峙等高冲击元素，但前提是它们必须与原场景及本集后续剧情兼容，不能凭空新增大事件。
5. 保留原有结构化格式，尽量沿用场次标题与时间/地点/人物等字段。
6. 语言要更狠、更抓人、更有张力，允许轻微疯感，但不能写成脱离原剧情的另一个故事。
7. 输出长度必须尽量贴近原场景，字数浮动不能超过原文的 10%。
8. 不要解释，不要分析，不要加引号或代码块，只输出改写后的“第一个场景”全文。
"""

LENGTH_FIX_SYSTEM_PROMPT = """你是一个极其严格的剧本润色编辑。

你只负责在不改变剧情事实、不改变结构字段的前提下，微调一个场景的长度。

必须严格遵守：
1. 只能微调字数，不得改剧情走向。
2. 必须保留场次标题、时间/地点/人物等结构字段。
3. 输出字数必须落在给定范围内。
4. 不要解释，不要分析，只输出修正后的完整场景。
"""

AUDIT_SYSTEM_PROMPT = """你是严格的短剧审稿编辑，请审核一个“首场强化改写”是否合格。

只返回一个 JSON 对象，不要返回 Markdown，不要附加解释。

JSON 字段要求：
{
  "verdict": "pass" | "warn" | "fail",
  "hook_score": 1-5,
  "consistency_score": 1-5,
  "format_ok": true,
  "plot_ok": true,
  "summary": "不超过40字的中文结论"
}

判定标准：
- pass：开篇抓人且未偏离原剧情，格式基本稳定。
- warn：总体可用，但冲击力不够、格式略有瑕疵或存在轻微风险。
- fail：明显偏离剧情、格式严重跑偏、关键信息缺失，或无法作为该集首场。
"""


@dataclass(frozen=True)
class EpisodeAudit:
    episode_heading: str
    original_length: int
    rewritten_length: int
    min_length: int
    max_length: int
    length_ok: bool
    verdict: str
    hook_score: int | None
    consistency_score: int | None
    format_ok: bool
    plot_ok: bool
    fallback_used: bool
    summary: str
    attempts_used: int

    def to_dict(self) -> dict[str, object]:
        return {
            "episode_heading": self.episode_heading,
            "original_length": self.original_length,
            "rewritten_length": self.rewritten_length,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "length_ok": self.length_ok,
            "verdict": self.verdict,
            "hook_score": self.hook_score,
            "consistency_score": self.consistency_score,
            "format_ok": self.format_ok,
            "plot_ok": self.plot_ok,
            "fallback_used": self.fallback_used,
            "summary": self.summary,
            "attempts_used": self.attempts_used,
        }


@dataclass(frozen=True)
class RewriteResult:
    title: str
    content: str
    download_name: str
    episode_count: int
    provider: str
    audits: list[EpisodeAudit]
    completed_count: int


class RewriteStoppedError(RuntimeError):
    def __init__(self, message: str, partial_result: RewriteResult) -> None:
        super().__init__(message)
        self.partial_result = partial_result


class EpisodeRetryExhausted(RuntimeError):
    def __init__(self, message: str, last_audit: EpisodeAudit) -> None:
        super().__init__(message)
        self.last_audit = last_audit


class ScriptRewriter:
    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def rewrite_script(self, script_text: str, provider_name: str) -> RewriteResult:
        return self.rewrite_script_progressive(script_text, provider_name, progress_callback=None)

    def rewrite_script_progressive(
        self,
        script_text: str,
        provider_name: str,
        progress_callback: Callable[[RewriteResult], None] | None,
    ) -> RewriteResult:
        parsed = parse_script(script_text)
        rewritten_episodes: list[str] = []
        audits: list[EpisodeAudit] = []

        for index, episode in enumerate(parsed.episodes):
            try:
                rewritten_episode, audit = self._rewrite_episode_until_pass(
                    parsed=parsed,
                    episode_heading=episode.heading,
                    episode_content=episode.content,
                    provider_name=provider_name,
                )
            except EpisodeRetryExhausted as exc:
                failed_audit = EpisodeAudit(
                    episode_heading=exc.last_audit.episode_heading,
                    original_length=exc.last_audit.original_length,
                    rewritten_length=exc.last_audit.rewritten_length,
                    min_length=exc.last_audit.min_length,
                    max_length=exc.last_audit.max_length,
                    length_ok=exc.last_audit.length_ok,
                    verdict=exc.last_audit.verdict,
                    hook_score=exc.last_audit.hook_score,
                    consistency_score=exc.last_audit.consistency_score,
                    format_ok=exc.last_audit.format_ok,
                    plot_ok=exc.last_audit.plot_ok,
                    fallback_used=True,
                    summary=f"{exc.last_audit.summary}，多次重写仍未通过，已保留原文并中止任务",
                    attempts_used=exc.last_audit.attempts_used,
                )
                failure_result = self._build_result(
                    parsed=parsed,
                    provider_name=provider_name,
                    rewritten_episodes=rewritten_episodes,
                    audits=audits + [failed_audit],
                    completed_count=len(rewritten_episodes),
                    remaining_episodes=parsed.episodes[index:],
                )
                raise RewriteStoppedError(str(exc), failure_result) from exc

            rewritten_episodes.append(rewritten_episode)
            audits.append(audit)
            snapshot = self._build_result(
                parsed=parsed,
                provider_name=provider_name,
                rewritten_episodes=rewritten_episodes,
                audits=audits,
                completed_count=len(rewritten_episodes),
                remaining_episodes=parsed.episodes[index + 1 :],
            )
            if progress_callback:
                progress_callback(snapshot)

        return self._build_result(
            parsed=parsed,
            provider_name=provider_name,
            rewritten_episodes=rewritten_episodes,
            audits=audits,
            completed_count=len(rewritten_episodes),
            remaining_episodes=[],
        )

    def _rewrite_episode_until_pass(
        self,
        parsed: ParsedScript,
        episode_heading: str,
        episode_content: str,
        provider_name: str,
    ) -> tuple[str, EpisodeAudit]:
        scene_split = split_first_scene(episode_content)
        normalized_heading = episode_heading.strip()
        if not scene_split.first_scene.strip():
            audit = EpisodeAudit(
                episode_heading=normalized_heading,
                original_length=0,
                rewritten_length=0,
                min_length=0,
                max_length=0,
                length_ok=False,
                verdict="warn",
                hook_score=None,
                consistency_score=None,
                format_ok=False,
                plot_ok=False,
                fallback_used=True,
                summary="未识别到首场，已保留原文",
                attempts_used=0,
            )
            return f"{episode_heading}{episode_content}", audit

        original_scene = scene_split.first_scene
        retry_feedback = ""
        last_audit: EpisodeAudit | None = None

        for attempt in range(1, MAX_REWRITE_ATTEMPTS + 1):
            user_prompt = self._build_user_prompt(
                parsed=parsed,
                episode_heading=normalized_heading,
                first_scene=original_scene.strip(),
                full_episode=f"{scene_split.prelude}{scene_split.first_scene}{scene_split.remainder}".strip(),
                next_content=scene_split.remainder.strip(),
                retry_feedback=retry_feedback,
                attempt=attempt,
            )

            logger.info("开始改写 %s 的首场，第 %s 次尝试", normalized_heading, attempt)
            rewritten_scene = self.llm_client.chat(
                provider_name=provider_name,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                purpose=f"{normalized_heading} 首场改写（第{attempt}次）",
            )
            candidate_scene = self._normalize_scene_output(original_scene, rewritten_scene)
            candidate_scene, original_length, candidate_length, min_length, max_length = (
                self._enforce_length_range(
                    original_scene=original_scene,
                    rewritten_scene=candidate_scene,
                    provider_name=provider_name,
                    episode_heading=normalized_heading,
                )
            )

            audit = self._audit_scene(
                provider_name=provider_name,
                episode_heading=normalized_heading,
                original_scene=original_scene,
                rewritten_scene=candidate_scene,
                original_length=original_length,
                rewritten_length=candidate_length,
                min_length=min_length,
                max_length=max_length,
                attempts_used=attempt,
            )
            last_audit = audit
            if self._qualifies_as_pass(audit):
                return (
                    f"{episode_heading}{scene_split.prelude}{candidate_scene}{scene_split.remainder}",
                    audit,
                )
            retry_feedback = self._build_retry_feedback(audit, original_scene, candidate_scene)
            logger.warning(
                "%s 首场第 %s 次尝试未通过 verdict=%s summary=%s",
                normalized_heading,
                attempt,
                audit.verdict,
                audit.summary,
            )

        if last_audit is None:
            raise EpisodeRetryExhausted(f"{normalized_heading} 首场改写失败", self._empty_audit(normalized_heading))
        raise EpisodeRetryExhausted(f"{normalized_heading} 首场在 {MAX_REWRITE_ATTEMPTS} 次重写后仍未通过审核", last_audit)

    def _enforce_length_range(
        self,
        original_scene: str,
        rewritten_scene: str,
        provider_name: str,
        episode_heading: str,
    ) -> tuple[str, int, int, int, int]:
        original_length = count_visible_chars(original_scene)
        min_length, max_length = scene_length_range(original_length)
        rewritten_length = count_visible_chars(rewritten_scene)
        if min_length <= rewritten_length <= max_length:
            return rewritten_scene, original_length, rewritten_length, min_length, max_length

        direction = "压缩" if rewritten_length > max_length else "扩写"
        logger.warning(
            "%s 首场字数超出限制，准备二次修正: original=%s rewritten=%s range=%s-%s",
            episode_heading,
            original_length,
            rewritten_length,
            min_length,
            max_length,
        )
        fixed_scene = self.llm_client.chat(
            provider_name=provider_name,
            system_prompt=LENGTH_FIX_SYSTEM_PROMPT,
            user_prompt=f"""请把下面这个场景做长度微调。

【当前集】
{episode_heading}

【原场景原文字数】
{original_length}

【允许字数范围】
{min_length} 到 {max_length}

【当前问题】
当前改写稿需要{direction}，因为它超出了允许字数范围。

【原场景】
{original_scene.strip()}

【待修正改写稿】
{rewritten_scene.strip()}

请输出修正后的完整场景，必须保留原格式，并把字数控制在范围内。""",
            purpose=f"{episode_heading} 长度修正",
        )
        normalized_fixed_scene = self._normalize_scene_output(original_scene, fixed_scene)
        fixed_length = count_visible_chars(normalized_fixed_scene)
        return normalized_fixed_scene, original_length, fixed_length, min_length, max_length

    def _audit_scene(
        self,
        provider_name: str,
        episode_heading: str,
        original_scene: str,
        rewritten_scene: str,
        original_length: int,
        rewritten_length: int,
        min_length: int,
        max_length: int,
        attempts_used: int,
    ) -> EpisodeAudit:
        length_ok = min_length <= rewritten_length <= max_length
        try:
            raw_audit = self.llm_client.chat(
                provider_name=provider_name,
                system_prompt=AUDIT_SYSTEM_PROMPT,
                user_prompt=f"""请审核下面这个“首场改写”。

【当前集】
{episode_heading}

【审核重点】
- 是否做到开篇更抓人、更快进入冲突
- 是否保持原剧情事实与人物关系
- 是否保留结构化格式
- 改写是否适合作为本集第一个场景

【原场景】
{original_scene.strip()}

【改写后场景】
{rewritten_scene.strip()}
""",
                temperature=0.2,
                max_tokens=600,
                purpose=f"{episode_heading} 审核",
            )
            parsed_audit = parse_audit_json(raw_audit)
            verdict = normalize_verdict(parsed_audit.get("verdict"))
            hook_score = clamp_score(parsed_audit.get("hook_score"))
            consistency_score = clamp_score(parsed_audit.get("consistency_score"))
            format_ok = to_bool(parsed_audit.get("format_ok"), default=True)
            plot_ok = to_bool(parsed_audit.get("plot_ok"), default=True)
            summary = str(parsed_audit.get("summary") or "审核完成").strip()
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s 首场审核失败", episode_heading)
            verdict = "warn"
            hook_score = None
            consistency_score = None
            format_ok = True
            plot_ok = True
            summary = f"审核失败：{exc}"

        if not length_ok:
            summary = f"字数超限；{summary}"
            verdict = "fail"

        return EpisodeAudit(
            episode_heading=episode_heading,
            original_length=original_length,
            rewritten_length=rewritten_length,
            min_length=min_length,
            max_length=max_length,
            length_ok=length_ok,
            verdict=verdict,
            hook_score=hook_score,
            consistency_score=consistency_score,
            format_ok=format_ok,
            plot_ok=plot_ok,
            fallback_used=False,
            summary=summary[:120],
            attempts_used=attempts_used,
        )

    @staticmethod
    def _normalize_scene_output(original_scene: str, model_output: str) -> str:
        cleaned = clean_model_output(model_output)
        restored = restore_scene_heading(original_scene, cleaned)
        return preserve_scene_whitespace(original_scene, restored)

    @staticmethod
    def _qualifies_as_pass(audit: EpisodeAudit) -> bool:
        return (
            audit.verdict == "pass"
            and audit.length_ok
            and audit.plot_ok
            and audit.format_ok
        )

    @staticmethod
    def _build_retry_feedback(audit: EpisodeAudit, original_scene: str, candidate_scene: str) -> str:
        issues = []
        if audit.verdict != "pass":
            issues.append(f"审核结论为 {audit.verdict}")
        if not audit.length_ok:
            issues.append(
                f"字数 {audit.rewritten_length} 不在允许范围 {audit.min_length}-{audit.max_length}"
            )
        if not audit.plot_ok:
            issues.append("剧情事实或人物关系有偏移")
        if not audit.format_ok:
            issues.append("结构化格式不稳定")
        if not issues and audit.summary:
            issues.append(audit.summary)
        return f"""上一次改写未通过，请你只修这些问题：
- {'；'.join(issues) or '请提升通过率'}
- 审核摘要：{audit.summary}

原场景如下：
{original_scene.strip()}

上一次失败稿如下：
{candidate_scene.strip()}"""

    def _build_result(
        self,
        parsed: ParsedScript,
        provider_name: str,
        rewritten_episodes: list[str],
        audits: list[EpisodeAudit],
        completed_count: int,
        remaining_episodes: list[EpisodeBlock],
    ) -> RewriteResult:
        tail = [f"{episode.heading}{episode.content}" for episode in remaining_episodes]
        content = f"{parsed.script_prefix}{parsed.body_prefix}{''.join(rewritten_episodes)}{''.join(tail)}"
        return RewriteResult(
            title=parsed.title,
            content=content,
            download_name=parsed.download_name,
            episode_count=len(parsed.episodes),
            provider=provider_name,
            audits=audits,
            completed_count=completed_count,
        )

    @staticmethod
    def _build_user_prompt(
        parsed: ParsedScript,
        episode_heading: str,
        first_scene: str,
        full_episode: str,
        next_content: str,
        retry_feedback: str,
        attempt: int,
    ) -> str:
        story_summary = parsed.sections.get("故事梗概", "")
        characters = parsed.sections.get("人物小传", "")
        core_scenes = parsed.sections.get("核心场景", "")
        tail_excerpt = next_content[:4000] if next_content else "无"
        full_episode_excerpt = full_episode[:9000]
        retry_block = f"\n【上一次审核问题】\n{retry_feedback}\n" if retry_feedback else ""
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

【当前尝试次数】
第 {attempt} 次

【本集全文参考】
{full_episode_excerpt}

【后续剧情衔接参考】
{tail_excerpt}

【需要改写的第一个场景原文】
{first_scene}
{retry_block}
改写目标：
- 开头第一眼就抓住观众，做到“开篇即爆”。
- 把最强冲突、羞辱、危险、压迫或反转尽量前置。
- 爽点和危机要更明确，语言更有张力，允许轻微疯感。
- 但绝不能偏离本集原有剧情，不要改后续发生的事实。
- 保留场景结构化字段和格式习惯。
- 改写后字数必须控制在原文的 10% 浮动范围内。
- 如果上一次审核指出了问题，这一次必须全部修掉。

现在直接输出改写后的第一个场景全文。"""

    @staticmethod
    def _empty_audit(episode_heading: str) -> EpisodeAudit:
        return EpisodeAudit(
            episode_heading=episode_heading,
            original_length=0,
            rewritten_length=0,
            min_length=0,
            max_length=0,
            length_ok=False,
            verdict="fail",
            hook_score=None,
            consistency_score=None,
            format_ok=False,
            plot_ok=False,
            fallback_used=True,
            summary="未生成有效审核结果",
            attempts_used=MAX_REWRITE_ATTEMPTS,
        )


def count_visible_chars(text: str) -> int:
    return len(NON_WHITESPACE_PATTERN.sub("", text))


def scene_length_range(original_length: int) -> tuple[int, int]:
    if original_length <= 0:
        return 0, 0
    delta = max(1, math.ceil(original_length * 0.1))
    min_length = max(1, original_length - delta)
    max_length = original_length + delta
    return min_length, max_length


def parse_audit_json(raw_text: str) -> dict[str, object]:
    cleaned = clean_model_output(raw_text)
    match = JSON_OBJECT_PATTERN.search(cleaned)
    candidate = match.group(0) if match else cleaned
    return json.loads(candidate)


def normalize_verdict(value: object) -> str:
    verdict = str(value or "warn").strip().lower()
    if verdict not in {"pass", "warn", "fail"}:
        return "warn"
    return verdict


def clamp_score(value: object) -> int | None:
    if value is None:
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(score, 5))


def to_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default
