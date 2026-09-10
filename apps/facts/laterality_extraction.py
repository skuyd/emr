"""Carry selected anatomy ranges forward; never relocate a phrase after save."""
from .laterality_schema import SCOPED_KEY


def scope_candidates(view, parent, site_start, site_end, side):
    from .clinical_extraction import Candidate, SITE, _candidate, _laterality

    if not parent.start <= site_start < site_end <= parent.end or parent.transformations:
        return []
    site = parent.value['text']
    if side:
        members = [{'member_key': 'whole', 'site_text': site, 'code': side, 'raw': view.raw(site_start, site_end)}]
        pieces = [view.fragments(site_start, site_end)]
        child = _candidate(view, 'lesion.laterality', {'code': side, 'raw': site}, parent.start, parent.end, entity=parent.entity)
        child.scope = {'scope_kind': 'WHOLE_ENTITY', 'members': members, 'pieces': pieces}
        return [child]
    members, pieces = [], []
    for match in SITE.finditer(view.text[site_start:site_end]):
        code = _laterality(match.group())
        if code is None:
            continue
        left, right = site_start + match.start(), site_start + match.end()
        raw = view.raw(left, right)
        members.append({'member_key': f'member:{len(members) + 1:03}', 'site_text': raw, 'code': code, 'raw': raw})
        pieces.append(view.fragments(left, right))
    if not members or len(members) > 32:
        return []
    fragments = [piece for group in pieces for piece in group]
    child = Candidate(SCOPED_KEY, parent.entity, {'scope': 'NAMED_MEMBERS_ONLY', 'members': members},
        fragments, '\n'.join(piece.text for piece in fragments), limitations=('side_applies_only_to_named_members',),
        start=site_start, end=site_end)
    child.scope = {'scope_kind': 'NAMED_MEMBERS_ONLY', 'members': members, 'pieces': pieces}
    return [child]
