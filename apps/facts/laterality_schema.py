"""Literal side qualifiers never flatten a mixed group into one side."""
import re

from django.core.exceptions import ValidationError


SCOPED_KEY = 'lesion.scoped_laterality'
SIDE_KEYS = {'lesion.laterality', SCOPED_KEY}
SIDES = {'LEFT': '左侧', 'RIGHT': '右侧', 'BILATERAL': '双侧', 'MIDLINE': '原文明示中线'}


def validate_scoped_value(value):
    if (not isinstance(value, dict) or set(value) != {'scope', 'members'}
            or value['scope'] != 'NAMED_MEMBERS_ONLY' or not isinstance(value['members'], list)
            or not 1 <= len(value['members']) <= 32):
        raise ValidationError('请保留一至三十二个原文明示部位及其独立侧别。')
    keys = []
    for member in value['members']:
        if not isinstance(member, dict) or set(member) != {'member_key', 'site_text', 'code', 'raw'}:
            raise ValidationError('部位侧别的成员形状无效。')
        if (not isinstance(member['member_key'], str) or not re.fullmatch(r'member:[0-9]{3}', member['member_key'])
                or member['code'] not in SIDES
                or any(not isinstance(member[key], str) or not member[key].strip() or len(member[key]) > 512
                       for key in ('site_text', 'raw'))):
            raise ValidationError('成员须有独立标识、原文部位和明确的侧别。')
        keys.append(member['member_key'])
    if len(set(keys)) != len(keys):
        raise ValidationError('部位侧别成员不能重复。')
    return value


def display_scoped_value(value):
    return '仅限列明部位：' + '；'.join(f"{member['site_text']}（{SIDES[member['code']]}）" for member in value['members'])


def member_identity(value):
    return [(member['member_key'], member['site_text']) for member in value['members']]
