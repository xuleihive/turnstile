"""Guards the locale catalogs against strings that exist only in the source language.

The UI is written in Simplified Chinese and translated at runtime by replacing text nodes.
A miss is therefore not a build error and not an obvious blank: the fallback substitutes
word by word and produces mixed output such as `Model未授权`. That has shipped twice, both
times because a new string was added to a component and not to the catalogs.

The check is deliberately substring-based rather than exact-key-based: a phrase may be
covered by an entry in ENGLISH_PHRASES or by one of the DYNAMIC_RULES regexes, and parsing
TypeScript regex literals from Python to tell which would be more fragile than the bug it
protects against.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE

FRONTEND_SRC = FRONTEND_SOURCE
LOCALES = FRONTEND_SRC / "locales"
HAN = re.compile(r"[\u4e00-\u9fff]")

# Short fragments are usually glued to a number or another phrase by a dynamic rule, so a
# containment test on them is noise. Four or more Han characters is reliably a whole phrase.
MIN_HAN_CHARACTERS = 4
LOCAL_IMPORT = re.compile(r'''from\s+["'](\./[^"']+)["']''')


def _catalog_source(locale: str) -> str:
    def read_module(path: Path, stack: tuple[Path, ...]) -> str:
        resolved = path.resolve()
        if resolved in stack:
            raise AssertionError(f"Circular locale import: {resolved}")
        source = resolved.read_text(encoding="utf-8")
        imported = []
        for target in LOCAL_IMPORT.findall(source):
            module = resolved.parent / target
            if module.suffix == "":
                module = module.with_suffix(".ts")
            imported.append(read_module(module, (*stack, resolved)))
        return "\n".join([*imported, source])

    return read_module(LOCALES / f"{locale}.ts", ())


def _source_literals() -> set[str]:
    literals: set[str] = set()
    for path in sorted(FRONTEND_SRC.rglob("*.tsx")):
        if LOCALES in path.parents:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            # A few components pick a pre-translated string per locale inline. Those are
            # already in the target language and must not be looked up in the catalogs.
            if "locale ===" in line:
                continue
            for pattern in (r'"([^"\n]*)"', r"'([^'\n]*)'", r">([^<>{}\n]+)<"):
                for match in re.findall(pattern, line):
                    candidate = match.strip()
                    if len(HAN.findall(candidate)) < MIN_HAN_CHARACTERS:
                        continue
                    literals.add(candidate)
    return literals


def test_every_source_phrase_is_covered_by_each_catalog() -> None:
    literals = _source_literals()
    # A regression that empties this would make the test vacuously pass.
    assert len(literals) > 100

    for locale in ("en", "ko", "ja"):
        catalog = _catalog_source(locale)
        missing = sorted(phrase for phrase in literals if phrase not in catalog)
        assert not missing, (
            f"{locale}.ts is missing {len(missing)} source phrase(s); "
            f"they will render as mixed text. First few: {missing[:5]}"
        )


def test_assistant_starter_prompts_enter_the_composer_in_the_active_locale() -> None:
    conversation = (
        FRONTEND_SRC / "components" / "assistant" / "assistant-conversation.tsx"
    ).read_text(encoding="utf-8")

    assert 'import { translateForLocale, useLocale } from "../../locales/index"' in conversation
    assert "const { locale } = useLocale()" in conversation
    assert "translateForLocale(starter.text, locale)" in conversation
    assert "setQuestion(translatedPrompt)" in conversation
    assert "setQuestion(starter.text)" not in conversation


def test_standalone_labels_have_exact_catalog_entries() -> None:
    labels = (
        # Attribution labels on the subscriptions and organization screens. Every one of these
        # is under MIN_HAN_CHARACTERS, so the coverage test above cannot see them -- and the
        # word-by-word fallback renders the miss rather than failing. `归属人` shipped to a live
        # environment as the column heading `归属people` before this list caught it.
        "归属人",
        "未指定",
        "未归属",
        "使用中",
        "重命名",
        "账号",
        "标识",
        "来自 APIM",
        "验证配置",
        "准备后端资源",
        "构建 APIM Revision",
        "验证候选 Revision",
        "切换 APIM Revision",
        "完成",
        "实际账单",
        "功能",
        "产品 / SKU",
        "待审核",
        "已批准",
        "已拒绝",
        "无席位",
        "活跃",
        "未保存",
        "标题",
        "全局",
        "Router 决策",
        "耗时",
        "正在读取",
        "APIM 后端池",
        "添加后端池",
        "从已有模型中选择并配置 APIM 后端池。",
        "搜索已有模型...",
        "搜索可添加后端池的模型",
        "已有模型列表",
        "选择其他模型",
        "已添加",
        "可添加",
        "副本",
        "正在读取后端池状态",
        "无法读取后端池状态",
        "缺少等价 Runtime",
        "缺少 Deployment",
        "缺少兼容 Runtime",
        "尚不能创建后端池",
        "该 Deployment 已作为物理副本使用。",
        "需要先在另一个兼容 Runtime 上登记同名上游 Deployment。",
        "需要先创建另一个兼容的受管 APIM Runtime。",
        "上游 Deployment",
        "当前 Runtime",
        "已登记部署",
        "所属后端池",
        "兼容目标 Runtime",
        "前往模型管理",
        "前往连接管理",
        "没有匹配的模型",
        "还没有后端池",
        "等价 Runtime",
        "数据面",
        "模型级 · 直调及所有 Router",
        "加权分流",
        "选择同一模型的物理 Deployment 如何接收流量。",
        "APIM 优先级",
        "权重 / 流量",
        "备用部署",
        "平台管理的 Circuit Breaker；仅 429 重试当前请求",
        "故障信号",
        "熔断时间",
        "最大尝试",
        "重试间隔",
        "首次快速重试",
        "后端超时",
        "停用 Pool",
        "停用 APIM 后端池？",
        "Retry-After · 最多 2 次",
    )
    for locale in ("en", "ko", "ja"):
        catalog = _catalog_source(locale)
        missing = [label for label in labels if f'"{label}":' not in catalog]
        assert not missing, (locale, missing)


def test_relative_time_is_formatted_by_intl_not_the_phrase_converter() -> None:
    """Dynamic timestamps must have one locale-aware formatting path.

    Phrase rules made `1 天前` depend on regex order: generic `1 天` consumed the front,
    leaving `1 day前`. Intl owns relative grammar and the rendered nodes opt out of the
    DOM translator, so neither side may quietly reintroduce the second path.
    """
    helper = (FRONTEND_SRC / "lib" / "time.ts").read_text(encoding="utf-8")
    assert "Intl.RelativeTimeFormat" in helper
    assert 'numeric: "always"' in helper

    for locale in ("en", "ko", "ja"):
        catalog = _catalog_source(locale)
        for obsolete in (r"\s*分钟前/g", r"\s*小时前/g", r"\s*天前/g"):
            assert obsolete not in catalog, (locale, obsolete)

    for relative_path in (
        FRONTEND_SRC / "components" / "assistant" / "assistant-panel.tsx",
        FRONTEND_SRC / "pages" / "assistant-page.tsx",
    ):
        source = relative_path.read_text(encoding="utf-8")
        assert 'className="assistant-history-meta" data-no-localize' in source
        assert "formatTimeAgo(item.updated_at, locale)" in source


# A JSX text run: what sits between a tag or an interpolation and the next one. Quote
# characters are excluded so that Chinese inside a ternary -- `{x ? "甲" : "乙"}` -- is not
# matched. That case is safe: the expression yields one string and React renders it as a
# single text node.
JSX_TEXT_RUN = re.compile(r"""(?P<open>[>}])(?P<text>[^<>{}\n"'`]*)(?P<close>[<{])""")

# Separators that live in the markup rather than in the phrase, so `拦截开关 ·` is looked
# up as `拦截开关`. The catalogs store the label; substring replacement puts the separator
# back untouched.
DECORATION = " \t·|/$%（）()，,。.：:、"


def test_a_sentence_is_never_split_by_a_jsx_interpolation() -> None:
    """The defect this catches has shipped three times, in three different pages.

    `<span>显示 {n} 项</span>` is not one string, it is three text nodes, and the provider
    walks text nodes. So a dynamic rule written for the whole sentence can never match it,
    and each fragment falls through to word-by-word substring replacement. The result is
    not obviously broken text either -- measured on the real translator, `再将可用预算平均
    分配给这` came out as `再将AvailableBudgetAverageAllocated给这`, because Chinese has no
    spaces to survive the substitution.

    Neither half of the sibling test above can see this: the extractor's own pattern
    excludes `{}`, and the phrase it would need to look up does not exist anywhere in the
    source as a literal.

    The fix is always the same shape -- make it one template literal, which is one text
    node, and give it a dynamic rule:

        <span>{`显示 ${n} 项`}</span>

    Note the lookup is for the *quoted* fragment. Plain containment is not enough and was
    tried first: the whole-sentence dynamic rule written for this very string lives in the
    catalog as a bare regex literal, so the fragment cut out of that sentence is trivially
    "present" and the check passed on the defect it exists to find. A catalog entry is
    quoted; a regex body is not, and only the entry can translate a lone text node.
    """
    catalogs = {
        locale: _catalog_source(locale)
        for locale in ("en", "ko", "ja")
    }
    split: list[str] = []

    for path in sorted(FRONTEND_SRC.rglob("*.tsx")):
        if LOCALES in path.parents:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "locale ===" in line:
                continue
            for match in JSX_TEXT_RUN.finditer(line):
                # Only runs that touch an interpolation can have been cut out of a longer
                # sentence; a run between two tags is already whole.
                if match["open"] != "}" and match["close"] != "{":
                    continue
                fragment = match["text"].strip()
                # Same threshold as above: shorter runs are labels, not clauses, and a
                # label beside a number is the normal way to write one.
                if len(HAN.findall(fragment)) < MIN_HAN_CHARACTERS:
                    continue
                # Both forms, because punctuation cuts both ways: `拦截开关 ·` is stored
                # without its separator, while `保存失败：` is stored with its colon.
                forms = {f'"{fragment}"', f'"{fragment.strip(DECORATION)}"'}
                if any(
                    not any(form in catalog for form in forms) for catalog in catalogs.values()
                ):
                    split.append(f"{path.relative_to(FRONTEND_SRC)}:{number}  {fragment!r}")

    assert not split, (
        "These Chinese runs sit next to a JSX interpolation and are not catalog entries, "
        "so each will be translated word by word and glued together. Make the whole "
        "sentence one template literal and add a dynamic rule:\n  " + "\n  ".join(split)
    )
