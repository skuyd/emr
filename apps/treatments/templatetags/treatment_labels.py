from django import template
from apps.treatments.forms import KINDS, OCCURRENCES

register = template.Library()


@register.filter
def treatment_kind(value):
    return dict(KINDS).get(value, "其他治疗记录")


@register.filter
def occurrence_label(value):
    return dict(OCCURRENCES).get(value, "发生状态不明")


@register.filter
def cycle_metric(value):
    return {"LAB_NEUT_COUNT": "ANC 中性粒细胞计数", "LAB_PLT": "PLT 血小板计数", "LAB_HGB": "HGB 血红蛋白"}.get(value, value)
