"""The fixed words of an alert, per language (``[alerts] language = "en" | "ru"``).

The catalog holds only Kelpie's own template text: transition words, labels, units, the
"Kelpie did" lines and one short headline per rule that sends alerts. Text that rules,
agents or people wrote (a reason, what is needed, a task title) is never translated: it is
passed through as it is.

Each language is a plain dict. A key ending in ``.one``, ``.few``, ``.many`` or ``.other``
is a plural form (:func:`plural`); English uses ``one`` and ``other``, Russian all four
(``other`` for fractions). A rule without a ``headline.<rule>`` entry gets the first
sentence of its reason as the headline (see :mod:`kelpie.alerts.render`).
"""

from __future__ import annotations

from datetime import timedelta

from kelpie.core.actions import Severity

DEFAULT = "en"

ICONS = {
    Severity.CRITICAL: "\U0001f534",  # red circle
    Severity.WARNING: "\U0001f7e1",  # yellow circle
    Severity.INFO: "\U0001f535",  # blue circle
}
RESOLVED_ICON = "✅"  # white heavy check mark

EN: dict[str, str] = {
    # transitions, shown in the headline line
    "transition.escalated": "still open after {age}",
    "transition.escalated.bare": "still open",
    "transition.changed": "changed",
    "transition.resolved": "resolved after {age}",
    "transition.resolved.bare": "resolved",
    # labels
    "label.needed": "Needed",
    "label.why": "Why",
    "label.blocking": "Blocking",
    "label.none": "none",
    "label.open_task": "Open task",
    "label.oldest": "(oldest {age})",
    "label.more": "and {n} more",
    "label.kelpie_did": "Kelpie did",
    "label.rule": "Rule",
    "count.tasks.one": "{n} task",
    "count.tasks.other": "{n} tasks",
    # units
    "unit.hours": "{n} h",
    "unit.minutes": "{n} min",
    # what Kelpie did, when the engine sent no line of its own (never for "notified")
    "did.suggested": "nothing, mode suggest. A human decides.",
    "did.failed": "tried, and it failed. Needs a human.",
    "did.not_executed": "nothing, no executor could carry it out. Needs a human.",
    "did.done": "carried it out.",
    # headlines: one per rule that sends alerts; ``.digest`` when it lists tasks
    "headline.digest": "{headline}: {tasks} {oldest}",
    "headline.bottlenecks": "Work waits while agents are idle",
    "headline.checkpoints": "Uncommitted work left in a session",
    "headline.ci_economy": "CI minutes at risk",
    "headline.exit_contract": "Task left without a valid finish",
    "headline.loop_breaker": "Runaway run or spend over budget",
    "headline.needs_you": "Waiting for you",
    "headline.needs_you.digest": "Waiting for you: {tasks} {oldest}",
    "headline.no_progress": "Tokens spent with no progress",
    "headline.orphan_status": "Task with no owner",
    "headline.output_offenders": "Command floods agent context",
    "headline.ping_pong": "Review ping-pong",
    "headline.resources": "Resources running out",
    "headline.return_causes": "A recurring cause of task returns",
    "headline.stale_blocks": "Block Kelpie cannot clear",
    "headline.wait_condition": "Waiting task that will not wake",
}

RU: dict[str, str] = {
    "transition.escalated": "не решено уже {age}",
    "transition.escalated.bare": "не решено",
    "transition.changed": "изменилось",
    "transition.resolved": "решено спустя {age}",
    "transition.resolved.bare": "решено",
    "label.needed": "Нужно",
    "label.why": "Почему",
    "label.blocking": "Блокирует",
    "label.none": "ничего",
    "label.open_task": "Открыть задачу",
    "label.oldest": "(самая давняя — {age})",
    "label.more": "и ещё {n}",
    "label.kelpie_did": "Kelpie сделал",
    "label.rule": "Правило",
    "count.tasks.one": "{n} задача",
    "count.tasks.few": "{n} задачи",
    "count.tasks.many": "{n} задач",
    "count.tasks.other": "{n} задачи",
    "unit.hours": "{n} ч",
    "unit.minutes": "{n} мин",
    "did.suggested": "ничего, режим suggest. Решает человек.",
    "did.failed": "попытался, но не вышло. Нужен человек.",
    "did.not_executed": "ничего: это некому выполнить. Нужен человек.",
    "did.done": "выполнил.",
    "headline.digest": "{headline}: {tasks} {oldest}",
    "headline.bottlenecks": "Работа ждёт, агенты простаивают",
    "headline.checkpoints": "Незакоммиченная работа осталась в сессии",
    "headline.ci_economy": "Минуты CI под угрозой",
    "headline.exit_contract": "Задача завершена не по правилам",
    "headline.loop_breaker": "Зациклившийся запуск или превышен бюджет",
    "headline.needs_you": "Ждут вас",
    "headline.needs_you.digest": "Ждут вас: {tasks} {oldest}",
    "headline.no_progress": "Токены тратятся без прогресса",
    "headline.orphan_status": "У задачи нет владельца",  # noqa: RUF001 (Russian)
    "headline.output_offenders": "Команда засоряет контекст агентов",
    "headline.ping_pong": "Пинг-понг на ревью",
    "headline.resources": "Ресурсы на исходе",
    "headline.return_causes": "Повторяющаяся причина возвратов",
    "headline.stale_blocks": "Блок, который Kelpie не может снять",
    "headline.wait_condition": "Ожидание, которое не закончится",
}

CATALOG: dict[str, dict[str, str]] = {"en": EN, "ru": RU}
LANGUAGES = tuple(CATALOG)


def check_language(value: object, where: str = "[alerts] language") -> str:
    """The language code; a ``ValueError`` names the key."""
    if not isinstance(value, str) or value not in CATALOG:
        raise ValueError(f"{where} must be one of {list(LANGUAGES)}")
    return value


def text(lang: str, key: str, **values: object) -> str:
    """The catalog entry ``key`` in ``lang`` (English when it has none), filled in."""
    template = CATALOG.get(lang, EN).get(key)
    if template is None:
        template = EN[key]
    return template.format(**values) if values else template


def has(lang: str, key: str) -> bool:
    return key in CATALOG.get(lang, EN) or key in EN


def plural(lang: str, n: int) -> str:
    """The plural form of ``n``: ``one``/``other`` in English, ``one``/``few``/``many`` in
    Russian (1 задача, 2 задачи, 5 задач, 21 задача, 11 задач)."""
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"


def tasks(lang: str, n: int) -> str:
    """``2 tasks``, ``5 задач``."""
    return text(lang, f"count.tasks.{plural(lang, n)}", n=n)


def age(lang: str, value: timedelta) -> str:
    """A duration as a human reads it: whole minutes below an hour, hours with one decimal
    below ten, whole hours above (``45 min``, ``4.5 h``, ``44 h``; Russian ``4,5 ч``)."""
    seconds = max(0.0, value.total_seconds())
    if seconds < 3600:
        return text(lang, "unit.minutes", n=int(seconds // 60))
    hours = seconds / 3600
    if hours < 10:
        shown = f"{int(hours * 10) / 10:.1f}"
        shown = shown.replace(".", ",") if lang == "ru" else shown
    else:
        shown = str(int(hours))
    return text(lang, "unit.hours", n=shown)
