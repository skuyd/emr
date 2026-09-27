"""Build the public reference catalog from the explicitly approved workbook.

Run locally with openpyxl installed. Runtime and CI use the bundled JSON, never
the private sample directory. Only the reference table's columns A-F are read.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata


SOURCE_SHA256 = 'fc5965e7812fed70409180a0a6b23a02f90953f9bebe5e3f453b7a29ed8ea202'
ROOT = Path(__file__).resolve().parents[1]

# The reorganized workbook moves rows without changing existing indicator IDs.
# These current source rows retain their original identity row; others are unchanged.
IDENTITY_ROWS = {
    29: 30, 33: 34, 37: 38, 41: 42, 44: 45, 45: 46, 48: 49,
    49: 50, 50: 51, 51: 52, 52: 53, 53: 54, 54: 55, 55: 56,
    56: 57, 57: 58, 58: 59, 59: 60, 60: 63, 61: 64, 62: 65,
    63: 66, 65: 68, 66: 69, 67: 70, 68: 71, 69: 72, 70: 73,
    71: 74, 72: 75, 73: 76, 74: 77, 75: 78, 76: 79, 77: 80,
    78: 81, 79: 82, 80: 83, 81: 84, 82: 85, 83: 86, 84: 87,
    85: 88, 86: 89, 87: 90, 88: 91, 89: 92, 90: 93, 91: 94,
    92: 95, 93: 96, 94: 97, 95: 98, 96: 99, 97: 100, 98: 101,
    99: 102, 100: 103, 101: 104, 102: 105, 103: 106, 104: 107, 105: 108,
    106: 109, 107: 110, 108: 111, 109: 112, 110: 113, 111: 114, 112: 115,
    113: 116, 114: 117, 115: 118, 116: 119, 117: 120, 118: 121, 120: 123,
    122: 125, 123: 126, 127: 130, 129: 132, 130: 133, 131: 134, 132: 135,
    133: 136, 134: 137, 135: 138, 137: 140, 139: 142, 140: 143, 141: 144,
    142: 233, 143: 234, 144: 145, 145: 146, 149: 150, 150: 151, 154: 155,
    155: 231, 229: 235, 230: 236, 231: 237, 232: 238, 233: 239, 234: 240,
    235: 241, 236: 242, 237: 243, 238: 247, 239: 61, 240: 62,
}

# Exact identities already in the repository, keyed by their original source row.
# Counts and percentages stay separate; unsupported near names are not aliases.
LEGACY_CODES = {
    4: 'WBC', 5: 'NEUT_PERCENT', 6: 'LYMPH_PERCENT', 7: 'MONO_PERCENT',
    8: 'EO_PERCENT', 9: 'BASO_PERCENT', 10: 'NEUT_COUNT', 11: 'LYMPH_COUNT',
    12: 'MONO_COUNT', 13: 'EO_COUNT', 14: 'BASO_COUNT', 15: 'HGB', 17: 'RBC',
    19: 'HCT', 21: 'MCH', 22: 'MCHC', 23: 'MCV', 25: 'PLT', 26: 'PDW',
    27: 'PLATELETCRIT', 28: 'MPV', 29: 'CRP', 49: 'AFP', 50: 'CEA',
    51: 'CA125', 52: 'CA19_9', 53: 'CA153', 54: 'CA242', 55: 'CA50',
    56: 'CA724', 57: 'PSA', 58: 'NSE', 59: 'CYFRA21_1', 60: 'SCC',
    63: 'PROGRP', 66: 'FERRITIN', 68: 'T3', 69: 'T4', 70: 'FT3',
    71: 'FT4', 72: 'TSH', 78: 'URINE_PH', 79: 'URINE_SPECIFIC_GRAVITY',
    80: 'URINE_NITRITE', 81: 'URINE_GLUCOSE', 82: 'URINE_OCCULT_BLOOD',
    83: 'URINE_PROTEIN', 85: 'URINE_UROBILINOGEN', 86: 'URINE_KETONE',
    90: 'URINE_RBC', 91: 'URINE_WBC', 101: 'STOOL_WBC', 102: 'STOOL_RBC',
    110: 'STOOL_OCCULT_BLOOD', 111: 'PT', 114: 'INR', 115: 'FIB',
    116: 'TT', 118: 'APTT', 120: 'D_DIMER', 121: 'ALT', 123: 'AST',
    126: 'ALP', 130: 'GGT', 132: 'ADA', 133: 'TP', 134: 'ALB',
    135: 'GLOB', 136: 'A_G_RATIO', 137: 'CHE', 138: 'PA', 140: 'TBIL',
    142: 'DBIL', 143: 'IBIL', 144: 'TBA', 146: 'UREA', 150: 'RBP',
    151: 'CREA', 156: 'UA', 158: 'K', 159: 'NA', 160: 'CL', 161: 'CA',
    163: 'P', 165: 'MG', 166: 'SERUM_IRON', 168: 'CHOL', 169: 'TG',
    170: 'HDL_C', 171: 'LDL_C', 176: 'CK', 178: 'CK_MB', 179: 'LDH',
    180: 'NT_PROBNP', 181: 'HOMOCYSTEINE', 182: 'FASTING_GLUCOSE',
    184: 'HBA1C', 186: 'ESR', 187: 'PCT', 188: 'CRP',
    197: 'HIV_AB', 198: 'HCV_AB', 199: 'HBSAG', 200: 'HBSAB',
    201: 'HBEAG', 202: 'HBEAB', 203: 'HBCAB', 204: 'TP_AB',
    217: 'LYMPH_TOTAL_COUNT', 218: 'TOTAL_T_COUNT', 219: 'T_CELL_PERCENT',
    220: 'CD4_COUNT', 221: 'CD8_COUNT', 222: 'CD4_PERCENT', 223: 'CD8_PERCENT',
    225: 'B_CELL_PERCENT', 226: 'TOTAL_B_COUNT', 228: 'NK_CELL_PERCENT',
    229: 'ALT', 232: 'CREA', 234: 'LACTATE', 242: 'UACR',
}
EXPLICIT_ALIASES = {
    30: ['促卵泡生成激素', '促卵泡激素', 'FSH'],
    34: ['黄体生成素', '促黄体生成素', 'LH'], 38: ['雌二醇', 'E2'],
    42: ['孕酮', 'Progesterone'], 45: ['睾酮', 'Testosterone'],
    46: ['泌乳素', '催乳素', 'PRL'], 61: ['胃蛋白酶原Ⅰ', 'PGI'],
    62: ['胃蛋白酶原Ⅱ', 'PGII'], 64: ['异常凝血酶原', 'PIVKA-II', 'DCP'],
    65: ['人绒毛膜促性腺激素', 'HCG'],
    73: ['甲状腺过氧化物酶抗体', 'TPOAb'], 74: ['甲状腺球蛋白抗体', 'TGAb'],
    75: ['甲状腺球蛋白'], 76: ['颜色', '尿液颜色'], 99: ['颜色', '粪便颜色'],
    151: ['肌酐-Cr'], 188: ['C反应蛋白-CRP'],
    155: ['肾小球滤过率', 'GFR'], 183: ['胰岛素', 'Insulin'],
    189: ['IgG'], 190: ['IgA'], 191: ['IgM'], 192: ['C3'], 193: ['C4'],
    244: ['胃蛋白酶原Ⅰ', 'PGI'], 245: ['胃蛋白酶原Ⅱ', 'PGII'],
}


def normalized(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


def build(path):
    from openpyxl import load_workbook

    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != SOURCE_SHA256:
        raise ValueError('Workbook differs from the approved source; confirm its version first.')
    sheet = load_workbook(io.BytesIO(encoded), data_only=True)['数据收集']
    sources, groups, current = [], [], None
    for number, cells in enumerate(sheet.iter_rows(min_row=4, max_col=6, values_only=True), start=4):
        group, name, unit, condition, low, high = cells
        if group:
            group = str(group).replace('\n', '').strip()
            groups.append(group)
        if name:
            current = dict(row=number, group=groups[-1], name=str(name).replace('\n', '').strip(),
                           unit=str(unit or ''), ranges=[])
            sources.append(current)
        if current is not None:
            current['ranges'].append(dict(row=number, condition=str(condition or ''),
                                          low=None if low is None else str(low),
                                          high=None if high is None else str(high)))
    if len(sources) != 201 or len(groups) != 22:
        raise ValueError('Source coverage differs from the approved table.')
    legacy = json.loads((ROOT / 'apps/labs/dictionaries/phase-two.json').read_text(encoding='utf-8'))
    old = {item['code']: item for item in legacy['indicators']}
    indicators = []
    for source in sources:
        number = IDENTITY_ROWS.get(source['row'], source['row'])
        code = 'LAB_' + LEGACY_CODES[number] if number in LEGACY_CODES else f'LAB_CATALOG_{number:03d}'
        code = {244: 'LAB_CATALOG_061', 245: 'LAB_CATALOG_062'}.get(number, code)
        previous = old.get(code)
        aliases = [source['name'], *EXPLICIT_ALIASES.get(number, ())]
        if previous:
            aliases.extend([previous['standard_name'], *previous['aliases']])
        # Only explicit Chinese-name + printed abbreviation combinations are split.
        name = source['name']
        for part in re.split(r'或', name):
            aliases.append(part)
        if '-' in name and re.match(r'^[\u3400-\u9fff]+-', name):
            aliases.extend(name.split('-', 1))
        parenthetical = re.fullmatch(r'(.+?)[（(]([^()（）]+)[）)]', name)
        if parenthetical and re.fullmatch(r'[A-Za-z0-9 -]+', parenthetical[2]):
            aliases.extend(parenthetical.groups())
        aliases = list(dict.fromkeys(value.strip() for value in aliases if value.strip()))
        specimen = 'URINE' if source['group'].startswith('尿') else 'STOOL' if source['group'].startswith('大便') else 'BLOOD'
        standard_name = {76: '尿液颜色', 99: '粪便颜色', 121: '丙氨酸氨基转移酶',
                         29: 'C反应蛋白', 188: 'C反应蛋白', 151: '肌酐', 232: '肌酐',
                         61: '胃蛋白酶原Ⅰ', 244: '胃蛋白酶原Ⅰ',
                         62: '胃蛋白酶原Ⅱ', 245: '胃蛋白酶原Ⅱ'}.get(number, name)
        indicators.append(dict(code=code, name=standard_name, category=source['group'],
                               specimen=specimen, unit=source['unit'], aliases=aliases,
                               source_rows=[source['row']],
                               ranges=source['ranges']))
    aliases_by_code = {}
    for item in indicators:
        aliases_by_code.setdefault(item['code'], []).extend(item['aliases'])
    for item in indicators:
        item['aliases'] = list(dict.fromkeys(aliases_by_code[item['code']]))
    return dict(source_sha256=SOURCE_SHA256, sheet='数据收集', groups=groups,
                source_rows=sources, indicators=indicators)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'apps/labs/dictionaries/indicator-catalog.json')
    args = parser.parse_args()
    payload = build(args.workbook)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"Catalog written: {len(payload['source_rows'])} source entries, {len(payload['groups'])} groups")


if __name__ == '__main__':
    main()
