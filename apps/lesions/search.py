"""Current names are searchable; source-invalid links do not label documents."""

from collections import defaultdict

from django.urls import reverse

from .models import Lesion
from .readmodels import observation_material


def document_links(patient):
    """Trusted, already-authorized archive caller; no new relation is inferred."""
    result = defaultdict(dict)
    if Lesion.objects.filter(patient=patient).exists():
        for row in observation_material(patient):
            if not row["usable"]:
                continue
            identity = row["lesion_id"]
            result[row["document_id"]][identity] = {
                "id": identity, "name": row["lesion_name"],
                "url": reverse("lesions:detail", args=[identity]) + "?patient=" + str(patient.pk),
                "search_text": f'已核对的观察关联：{row["lesion_name"]} · {identity}',
            }
    return {document: tuple(values[key] for key in sorted(values)) for document, values in result.items()}


def observation_matches(row, query):
    needle = query.casefold()
    values = [row["label"], row["id"], row["lesion_id"] or "", row["lesion_name"]]
    values.extend(field["content"]["text"] for field in row["fields"] + row["context_fields"]
                  if field["source_valid"] and field["status"] != "EXCLUDED")
    return any(needle in value.casefold() for value in values)
