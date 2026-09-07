from urllib.parse import urlencode

from django import template
from django.urls import reverse


register = template.Library()


@register.simple_tag(takes_context=True)
def patient_url(context, name, *args, **kwargs):
    url = reverse(name, args=args, kwargs=kwargs)
    patient = getattr(context.get("request"), "patient", None)
    return url + "?" + urlencode({"patient": str(patient.pk)}) if patient is not None else url
