from django import template
from apps.treatments.forms import KINDS, OCCURRENCES
from apps.treatments.overlays import METRIC_LABELS

register = template.Library()


@register.filter
def treatment_kind(value):
    return dict(KINDS).get(value, "其他治疗记录")


@register.filter
def occurrence_label(value):
    return dict(OCCURRENCES).get(value, "发生状态不明")


@register.filter
def cycle_metric(value):
    return METRIC_LABELS.get(value, value)
