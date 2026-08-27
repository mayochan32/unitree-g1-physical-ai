"""
pc_pipeline.llm

会話履歴を保持しながらOpenAIのLLMで応答を作る。

2つの応答モードを持つ:
  respond()          … 応答が全部できてから返す（デバッグしやすい）
  stream_sentences() … 文が1つ完成するたびにyieldする（体感レイテンシが下がる）

ストリーミング側は、最初の一文ができた時点でTTS→再生に回せるので、
「発話終了から音が出るまで」の待ち時間を大きく削れる。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator

from .config import OpenAIConfig

# 日本語・英語の文末で区切る。読点では切らない（細切れになりすぎるため）。
_SENTENCE_END = re.compile(r"[。！？!?\n]+")

# これ以上溜まったら文末が来なくても一度吐き出す（長文の言いっぱなし対策）
_FORCE_FLUSH_CHARS = 60


class ConversationLLM:
    def __init__(
        self,
        config: OpenAIConfig,
        system_prompt: str,
        gdl_profile_path: str = "",
        client=None,
    ):
        self._config = config
        self._client = client or _build_client(config)
        self._system_prompt = _compose_system_prompt(system_prompt, gdl_profile_path)
        self._history: list[dict[str, str]] = []

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def reset(self) -> None:
        self._history.clear()

    def _messages(self, user_text: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            *self._history,
            {"role": "user", "content": user_text},
        ]

    def _remember(self, user_text: str, reply: str) -> None:
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": reply})

        # 直近N往復だけ残す（1往復 = 2メッセージ）
        limit = self._config.max_history_turns * 2
        if len(self._history) > limit:
            self._history = self._history[-limit:]

    def respond(self, user_text: str) -> str:
        """応答を一括で得る。"""
        completion = self._client.chat.completions.create(
            model=self._config.llm_model,
            messages=self._messages(user_text),
        )
        reply = (completion.choices[0].message.content or "").strip()
        self._remember(user_text, reply)
        return reply

    def stream_sentences(self, user_text: str) -> Iterator[str]:
        """
        応答を文単位でyieldする。呼び出し側は受け取った文から順にTTSに流せる。
        全部yieldし終えた時点で会話履歴に記録する。
        """
        stream = self._client.chat.completions.create(
            model=self._config.llm_model,
            messages=self._messages(user_text),
            stream=True,
        )

        buffer = ""
        full_reply = ""

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if not piece:
                continue

            buffer += piece
            full_reply += piece

            # 文末が現れたところで切り出す
            while True:
                match = _SENTENCE_END.search(buffer)
                if not match:
                    break
                sentence = buffer[: match.end()].strip()
                buffer = buffer[match.end() :]
                if sentence:
                    yield sentence

            # 文末が来ないまま長くなったら区切って吐く
            if len(buffer) >= _FORCE_FLUSH_CHARS:
                yield buffer.strip()
                buffer = ""

        tail = buffer.strip()
        if tail:
            yield tail

        self._remember(user_text, full_reply.strip())


def _compose_system_prompt(base_prompt: str, gdl_profile_path: str) -> str:
    """
    ベースのシステムプロンプトに、GDL（Ghost Description Language）の
    人格記述を追記する。これが gdl-integration テーマとの接続点になる。
    """
    if not gdl_profile_path:
        return base_prompt

    if not os.path.exists(gdl_profile_path):
        raise FileNotFoundError(f"GDLプロファイルが見つかりません: {gdl_profile_path}")

    with open(gdl_profile_path, "r", encoding="utf-8") as handle:
        profile = json.load(handle)

    return (
        f"{base_prompt}\n\n"
        "以下はあなたの人格を記述したGDL(Ghost Description Language)のプロファイルです。"
        "この人格として一貫した振る舞い・話し方をしてください。\n\n"
        f"{json.dumps(profile, ensure_ascii=False, indent=2)}"
    )


def _build_client(config: OpenAIConfig):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openaiパッケージが必要です: pip install openai") from exc

    if not config.api_key:
        raise RuntimeError("環境変数 OPENAI_API_KEY が設定されていません")

    return OpenAI(api_key=config.api_key)
