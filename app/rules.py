"""Декларативные правила, которые аналитик меняет без разработчиков.

Правило — список условий над признаками (app/features.py, FEATURES).
Значение условия может ссылаться на настройки страны: "$limits.large_transfer".
Никакого eval: условия разбираются и проверяются при загрузке.
"""
from __future__ import annotations

import operator
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .features import FEATURES

OPS = {
    "==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b,
}
SCENARIOS = {"social_engineering", "account_takeover", "compromised_cards", "droppers",
             "multiaccount", "stoplist", "limits", "data_quality"}


class Rule(BaseModel):
    id: str
    description: str
    scenario: str
    enabled: bool = True
    jurisdictions: list[str] = ["*"]
    when: list[list[Any]] = Field(description="[признак, оператор, значение]")
    points: int = 0
    action: Optional[Literal["challenge", "decline"]] = Field(None, description="Минимальное действие")
    case: bool = Field(False, description="Создать инцидент для разбора")

    @field_validator("scenario")
    @classmethod
    def known_scenario(cls, v: str) -> str:
        if v not in SCENARIOS:
            raise ValueError(f"неизвестный сценарий {v}; допустимо: {sorted(SCENARIOS)}")
        return v

    @model_validator(mode="after")
    def check_conditions(self) -> "Rule":
        if not self.when:
            raise ValueError(f"{self.id}: нужно хотя бы одно условие")
        for c in self.when:
            if len(c) != 3:
                raise ValueError(f"{self.id}: условие {c} должно быть [признак, оператор, значение]")
            feat, op, _ = c
            if feat not in FEATURES:
                raise ValueError(f"{self.id}: неизвестный признак «{feat}»")
            if op not in OPS:
                raise ValueError(f"{self.id}: неизвестный оператор «{op}»")
        if not -100 <= self.points <= 100:
            raise ValueError(f"{self.id}: баллы от -100 до 100")
        return self


class RuleSet(BaseModel):
    version: str
    ml_weight: int = Field(45, ge=0, le=100, description="Сколько баллов даёт ML при вероятности 1.0")
    rules: list[Rule]

    @model_validator(mode="after")
    def unique_ids(self) -> "RuleSet":
        ids = [r.id for r in self.rules]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"повторяются id правил: {sorted(dup)}")
        return self


def parse(text: str, jurisdictions: dict) -> RuleSet:
    data = yaml.safe_load(text)
    rs = RuleSet.model_validate(data)
    for r in rs.rules:
        for j in r.jurisdictions:
            if j != "*" and j not in jurisdictions:
                raise ValueError(f"{r.id}: неизвестная страна {j}")
        for _, _, v in r.when:
            if isinstance(v, str) and v.startswith("$"):
                for code, jur in jurisdictions.items():
                    _resolve(v, jur)  # бросит ошибку, если ссылки нет
    return rs


def _resolve(v: Any, jur: dict) -> Any:
    if isinstance(v, str) and v.startswith("$"):
        node: Any = jur
        for part in v[1:].split("."):
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"нет настройки страны {v}")
            node = node[part]
        return node
    return v


def evaluate(rs: RuleSet, f: dict, jurisdiction: str, jur: dict) -> list[Rule]:
    fired = []
    for r in rs.rules:
        if not r.enabled or ("*" not in r.jurisdictions and jurisdiction not in r.jurisdictions):
            continue
        ok = True
        for feat, op, val in r.when:
            a = f.get(feat)
            if a is None:
                ok = False
                break
            try:
                if not OPS[op](a, _resolve(val, jur)):
                    ok = False
                    break
            except TypeError:
                ok = False
                break
        if ok:
            fired.append(r)
    return fired
