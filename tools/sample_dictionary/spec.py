from dataclasses import dataclass
import re


@dataclass(frozen=True)
class IndicatorSpec:
    code: str
    standard_name: str
    selector_pattern: str
    aliases: tuple[str, ...]
    ocr_variants: tuple[str, ...] = ()
    unit_forms: tuple[str, ...] = ()
    category: str = "LABORATORY"
    capability_level: str = "STABLE"


def _lab(
    code,
    standard_name,
    selector_pattern,
    aliases,
    *,
    ocr=(),
    units=(),
    category="LABORATORY",
    capability="STABLE",
):
    return IndicatorSpec(
        code=code,
        standard_name=standard_name,
        selector_pattern=selector_pattern,
        aliases=tuple(aliases),
        ocr_variants=tuple(ocr),
        unit_forms=tuple(units),
        category=category,
        capability_level=capability,
    )


CONVENTIONAL_SPECS = (
    # Hematology: 17
    _lab("LAB_WBC", "白细胞计数", r"(^|\s)WBC(\s|$)|白细胞$", ("WBC", "白细胞", "WBC 白细胞"), units=("10^9/L", "×10^9/L", "10*9/L"), category="HEMATOLOGY"),
    _lab("LAB_RBC", "红细胞计数", r"(^|\s)RBC(\s|$)|红细胞$", ("RBC", "红细胞", "RBC 红细胞"), units=("10^12/L", "×10^12/L", "10*12/L"), category="HEMATOLOGY"),
    _lab("LAB_HGB", "血红蛋白", r"(^|\s)HGB(\s|$)|(^|、)血红蛋白$", ("HGB", "血红蛋白", "HGB 血红蛋白"), units=("g/L", "g/dL"), category="HEMATOLOGY"),
    _lab("LAB_HCT", "红细胞压积", r"(^|\s)HCT(\s|$)|红细胞压积|血细胞比容", ("HCT", "红细胞压积", "血细胞比容"), units=("%", "L/L"), category="HEMATOLOGY"),
    _lab("LAB_MCV", "平均红细胞体积", r"(^|\s)MCV(\s|$)|平均红细胞体积|红细胞平均体积", ("MCV", "平均红细胞体积", "红细胞平均体积"), units=("fL",), category="HEMATOLOGY"),
    _lab("LAB_MCH", "平均红细胞血红蛋白量", r"(^|\s)MCH(\s|$)|平均血红蛋白量", ("MCH", "平均血红蛋白量", "平均红细胞血红蛋白量"), units=("pg",), category="HEMATOLOGY"),
    _lab("LAB_MCHC", "平均红细胞血红蛋白浓度", r"MCHC|平均血红蛋白浓", ("MCHC", "平均血红蛋白浓度", "平均红细胞血红蛋白浓度"), ocr=("MCHC 平均血红蛋白浓",), units=("g/L", "g/dL"), category="HEMATOLOGY"),
    _lab("LAB_RDW_CV", "红细胞分布宽度变异系数", r"RDW-CV|红细胞分布宽度CV", ("RDW-CV", "红细胞分布宽度CV", "红细胞分布宽度变异系数"), units=("%",), category="HEMATOLOGY"),
    _lab("LAB_RDW_SD", "红细胞分布宽度标准差", r"RDW-SD|红细胞分布宽度SD", ("RDW-SD", "红细胞分布宽度SD", "红细胞分布宽度标准差"), units=("fL",), category="HEMATOLOGY"),
    _lab("LAB_PLT", "血小板计数", r"(^|\s)PLT(\s|$)|血小板$", ("PLT", "血小板", "血小板计数"), units=("10^9/L", "×10^9/L", "10*9/L"), category="HEMATOLOGY"),
    _lab("LAB_MPV", "平均血小板体积", r"(^|\s)MPV(\s|$)|平均血小板体积", ("MPV", "平均血小板体积"), units=("fL",), category="HEMATOLOGY"),
    _lab("LAB_NEUT_COUNT", "中性粒细胞计数", r"NEU#|中性粒细胞计数", ("NEU#", "NEUT#", "中性粒细胞计数"), units=("10^9/L", "×10^9/L"), category="HEMATOLOGY"),
    _lab("LAB_NEUT_PERCENT", "中性粒细胞百分比", r"NEUT%|中性粒细胞百分比", ("NEUT%", "NEU%", "中性粒细胞百分比"), units=("%",), category="HEMATOLOGY"),
    _lab("LAB_LYMPH_COUNT", "淋巴细胞计数", r"LYM#|淋巴细胞计数", ("LYM#", "LYMPH#", "淋巴细胞计数"), units=("10^9/L", "×10^9/L"), category="HEMATOLOGY"),
    _lab("LAB_LYMPH_PERCENT", "淋巴细胞百分比", r"LYMPH%|淋巴细胞百分比", ("LYM%", "LYMPH%", "淋巴细胞百分比"), units=("%",), category="HEMATOLOGY"),
    _lab("LAB_MONO_COUNT", "单核细胞计数", r"MON#|单核细胞计数", ("MON#", "MONO#", "单核细胞计数"), units=("10^9/L", "×10^9/L"), category="HEMATOLOGY"),
    _lab("LAB_MONO_PERCENT", "单核细胞百分比", r"MONO%|单核细胞百分比", ("MON%", "MONO%", "单核细胞百分比"), units=("%",), category="HEMATOLOGY"),

    # Proteins, liver and enzymes: 15
    _lab("LAB_TP", "总蛋白", r"(^|\s)TP\s+总蛋白|总蛋白$", ("TP", "总蛋白", "TP 总蛋白"), units=("g/L",), category="BIOCHEMISTRY"),
    _lab("LAB_ALB", "白蛋白", r"(^|\s)ALB(\s|$)|^白蛋白$", ("ALB", "白蛋白", "ALB 白蛋白"), units=("g/L",), category="BIOCHEMISTRY"),
    _lab("LAB_GLOB", "球蛋白", r"GLOB|^球蛋白$", ("GLOB", "球蛋白"), units=("g/L",), category="BIOCHEMISTRY"),
    _lab("LAB_A_G_RATIO", "白蛋白/球蛋白比值", r"A/G|白球比值", ("A/G", "白球比值", "白蛋白/球蛋白比值"), category="BIOCHEMISTRY"),
    _lab("LAB_ALT", "丙氨酸氨基转移酶", r"(^|\s)ALT(\s|$)|谷丙转氨酶", ("ALT", "谷丙转氨酶", "丙氨酸氨基转移酶"), units=("U/L",), category="BIOCHEMISTRY"),
    _lab("LAB_AST", "天门冬氨酸氨基转移酶", r"(^|\s)AST(\s|$)|谷草转氨酶|门冬氨酸氨基转移酶", ("AST", "谷草转氨酶", "天门冬氨酸氨基转移酶", "门冬氨酸氨基转移酶"), units=("U/L",), category="BIOCHEMISTRY"),
    _lab("LAB_ALP", "碱性磷酸酶", r"(^|\s)ALP(\s|$)|碱性磷酸酶", ("ALP", "碱性磷酸酶"), units=("U/L",), category="BIOCHEMISTRY"),
    _lab("LAB_GGT", "γ-谷氨酰转移酶", r"(^|\s)GGT(\s|$)|谷氨酰转肽酶", ("GGT", "γ-谷氨酰转移酶", "谷氨酰转肽酶"), units=("U/L",), category="BIOCHEMISTRY"),
    _lab("LAB_TBA", "总胆汁酸", r"(^|\s)TBA(\s|$)|总胆汁酸", ("TBA", "总胆汁酸"), units=("μmol/L", "umol/L"), category="BIOCHEMISTRY"),
    _lab("LAB_TBIL", "总胆红素", r"(^|\s)TBIL(\s|$)|总胆红素", ("TBIL", "总胆红素"), units=("μmol/L", "umol/L"), category="BIOCHEMISTRY"),
    _lab("LAB_DBIL", "直接胆红素", r"(^|\s)DBIL(\s|$)|直接胆红素", ("DBIL", "直接胆红素"), units=("μmol/L", "umol/L"), category="BIOCHEMISTRY"),
    _lab("LAB_IBIL", "间接胆红素", r"(^|\s)IBIL(\s|$)|间接胆红素", ("IBIL", "间接胆红素"), units=("μmol/L", "umol/L"), category="BIOCHEMISTRY"),
    _lab("LAB_CHE", "胆碱酯酶", r"(^|\s)CHE(\s|$)|胆碱酯酶", ("CHE", "胆碱酯酶"), units=("U/L", "kU/L"), category="BIOCHEMISTRY"),
    _lab("LAB_PA", "前白蛋白", r"(^|\d)PA\s+|前白蛋白", ("PA", "前白蛋白"), units=("mg/L", "g/L"), category="BIOCHEMISTRY"),
    _lab("LAB_ADA", "腺苷脱氨酶", r"(^|\s)ADA(\s|$)|腺苷脱氨酶", ("ADA", "腺苷脱氨酶"), units=("U/L",), category="BIOCHEMISTRY"),

    # Renal, osmolality and lipids: 10
    _lab("LAB_UREA", "尿素", r"UREA|尿素", ("UREA", "尿素", "BUN"), units=("mmol/L",), category="BIOCHEMISTRY"),
    _lab("LAB_CREA", "肌酐", r"CREA|肌酐", ("CREA", "肌酐", "Cr"), units=("μmol/L", "umol/L", "mg/dL"), category="BIOCHEMISTRY"),
    _lab("LAB_UREA_CREAT_RATIO", "尿素/肌酐比值", r"Urea/cr|尿素/肌", ("Urea/Cr", "尿素/肌酐", "尿素/肌酐比值"), ocr=("尿素/肌肝",), category="BIOCHEMISTRY", capability="EXPLORATORY"),
    _lab("LAB_UA", "尿酸", r"(^|\s)UA(\s|$)|尿酸$", ("UA", "尿酸"), units=("μmol/L", "umol/L", "mg/dL"), category="BIOCHEMISTRY"),
    _lab("LAB_OSM", "总渗透压", r"总渗透压|(^|\s)OSM(\s|$)", ("OSM", "总渗透压"), units=("mOsm/kg", "mOsm/kgH2O"), category="BIOCHEMISTRY", capability="EXPLORATORY"),
    _lab("LAB_CHOL", "总胆固醇", r"CHOL|总胆固醇", ("CHOL", "TC", "总胆固醇"), units=("mmol/L", "mg/dL"), category="BIOCHEMISTRY"),
    _lab("LAB_TG", "甘油三酯", r"(^|\s)TG(\s|$)|甘油三酯|甘油三脂", ("TG", "甘油三酯"), ocr=("甘油三脂",), units=("mmol/L", "mg/dL"), category="BIOCHEMISTRY"),
    _lab("LAB_HDL_C", "高密度脂蛋白胆固醇", r"HDL-C|高密度脂蛋白", ("HDL-C", "HDL", "高密度脂蛋白", "高密度脂蛋白胆固醇"), units=("mmol/L", "mg/dL"), category="BIOCHEMISTRY"),
    _lab("LAB_LDL_C", "低密度脂蛋白胆固醇", r"LDL-C|低密度脂蛋白", ("LDL-C", "LDL", "低密度脂蛋白", "低密度脂蛋白胆固醇"), units=("mmol/L", "mg/dL"), category="BIOCHEMISTRY"),
    _lab("LAB_NON_HDL_C", "非高密度脂蛋白胆固醇", r"non-HDL-C|非高密度脂蛋白胆固醇", ("non-HDL-C", "非高密度脂蛋白胆固醇"), units=("mmol/L", "mg/dL"), category="BIOCHEMISTRY"),

    # Cardiac and tissue enzymes: 4
    _lab("LAB_CK", "肌酸激酶", r"(^|\s)CK(\s|$)|肌酸激酶$", ("CK", "肌酸激酶"), units=("U/L",), category="BIOCHEMISTRY"),
    _lab("LAB_CK_MB", "肌酸激酶同工酶", r"CK-MB|肌酸激酶同工酶", ("CK-MB", "CKMB", "肌酸激酶同工酶"), units=("U/L", "ng/mL"), category="BIOCHEMISTRY"),
    _lab("LAB_LDH", "乳酸脱氢酶", r"(^|\s)LDH(\s|$)|乳酸脱氢酶", ("LDH", "乳酸脱氢酶"), units=("U/L",), category="BIOCHEMISTRY"),
    _lab("LAB_HBDH", "α-羟丁酸脱氢酶", r"HBDH|羟丁酸脱氢酶", ("HBDH", "α-HBDH", "α-羟丁酸脱氢酶", "羟丁酸脱氢酶"), units=("U/L",), category="BIOCHEMISTRY"),

    # Inflammation and tumour markers: 6
    _lab("LAB_CRP", "C反应蛋白", r"(^|\d)CRP\s+C反应蛋白$|^C反应蛋白$", ("CRP", "C反应蛋白"), units=("mg/L",), category="INFLAMMATION"),
    _lab("LAB_HS_CRP", "超敏C反应蛋白", r"hsCRP|超敏C反应蛋白", ("hsCRP", "hs-CRP", "超敏C反应蛋白"), units=("mg/L",), category="INFLAMMATION"),
    _lab("LAB_PCT", "降钙素原", r"(^|\s)PCT(\s|$)|降钙素原", ("PCT", "降钙素原"), units=("ng/mL",), category="INFLAMMATION"),
    _lab("LAB_AFP", "甲胎蛋白", r"(^|\s)AFP(\s|$)|甲胎蛋白", ("AFP", "甲胎蛋白"), units=("ng/mL", "IU/mL"), category="TUMOR_MARKER", capability="EXPLORATORY"),
    _lab("LAB_CEA", "癌胚抗原", r"(^|\s)CEA(\s|$)|癌胚抗原", ("CEA", "癌胚抗原"), units=("ng/mL",), category="TUMOR_MARKER", capability="EXPLORATORY"),
    _lab("LAB_CA19_9", "糖类抗原19-9", r"CA199|CA19-|糖类抗原CA19", ("CA19-9", "CA199", "糖类抗原19-9"), ocr=("糖类抗原CA19-",), units=("U/mL",), category="TUMOR_MARKER", capability="EXPLORATORY"),

    # Coagulation: 7
    _lab("LAB_PT", "凝血酶原时间", r"(^|\s)PT(\s|$)|凝血酶原时间", ("PT", "凝血酶原时间"), units=("s", "秒"), category="COAGULATION"),
    _lab("LAB_APTT", "活化部分凝血活酶时间", r"APTT|部分凝血", ("APTT", "活化部分凝血活酶时间"), units=("s", "秒"), category="COAGULATION"),
    _lab("LAB_TT", "凝血酶时间", r"(^|\s)TT(\s|$)|凝血酶时间", ("TT", "凝血酶时间"), units=("s", "秒"), category="COAGULATION"),
    _lab("LAB_FIB", "纤维蛋白原", r"(^|\s)(FIB|FBG)(\s|$)|纤维蛋白原", ("FIB", "FBG", "纤维蛋白原"), units=("g/L", "mg/dL"), category="COAGULATION"),
    _lab("LAB_D_DIMER", "D-二聚体", r"D-Dimer|D-二聚体", ("D-Dimer", "D-二聚体", "D二聚体"), units=("mg/L", "μg/mL", "ng/mL"), category="COAGULATION"),
    _lab("LAB_INR", "国际标准化比值", r"(^|\s)INR(\s|$)|国际标准化比值", ("INR", "国际标准化比值"), category="COAGULATION"),
    _lab("LAB_ATIII", "抗凝血酶III", r"ATIII|抗凝血酶III", ("ATIII", "AT-III", "抗凝血酶III"), units=("%",), category="COAGULATION"),

    # Hepatitis B serology and C-peptide: 6
    _lab("LAB_HBSAG", "乙型肝炎表面抗原", r"HBsA\s*g|乙肝表面抗原", ("HBsAg", "乙肝表面抗原", "乙型肝炎表面抗原"), ocr=("HBsA g", "乙肝表面抗原定最"), category="SEROLOGY", capability="EXPLORATORY"),
    _lab("LAB_HBSAB", "乙型肝炎表面抗体", r"ANTI-HBS|乙肝表面抗体", ("HBsAb", "anti-HBs", "乙肝表面抗体", "乙型肝炎表面抗体"), category="SEROLOGY", capability="EXPLORATORY"),
    _lab("LAB_HBEAG", "乙型肝炎e抗原", r"HBEAG|乙肝 e抗原", ("HBeAg", "乙肝e抗原", "乙型肝炎e抗原"), category="SEROLOGY", capability="EXPLORATORY"),
    _lab("LAB_HBEAB", "乙型肝炎e抗体", r"ANTI-HBE|乙肝 e抗体", ("HBeAb", "anti-HBe", "乙肝e抗体", "乙型肝炎e抗体"), category="SEROLOGY", capability="EXPLORATORY"),
    _lab("LAB_HBCAB", "乙型肝炎核心抗体", r"ANTI-HBC|乙肝核心抗体", ("HBcAb", "anti-HBc", "乙肝核心抗体", "乙型肝炎核心抗体"), category="SEROLOGY", capability="EXPLORATORY"),
    _lab("LAB_C_PEPTIDE", "C肽", r"(^|\s)C-P(\s|$)|C肽", ("C-P", "C肽", "C-peptide"), units=("ng/mL", "nmol/L"), category="ENDOCRINOLOGY", capability="EXPLORATORY"),

    # Lymphocyte subsets: 10
    _lab("LAB_B_CELL_PERCENT", "B淋巴细胞百分比", r"CD19\+%|总B淋巴细胞%", ("CD19+%", "B淋巴细胞百分比", "总B淋巴细胞%"), units=("%",), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_T_CELL_PERCENT", "T淋巴细胞百分比", r"CD45\+CD3\+|总T淋巴细胞%", ("CD45+CD3+", "T淋巴细胞百分比", "总T淋巴细胞%"), units=("%",), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_NK_CELL_PERCENT", "自然杀伤细胞百分比", r"CD3-CD16\+CD56\+|NK细胞%", ("CD3-CD16+CD56+", "NK细胞%", "自然杀伤细胞百分比"), units=("%",), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_CD4_PERCENT", "CD4阳性T细胞百分比", r"CD4 辅助性T细胞%", ("CD4%", "辅助性T细胞%", "CD4阳性T细胞百分比"), units=("%",), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_CD4_COUNT", "CD4阳性T细胞计数", r"CD4\+ 辅助性T细胞数", ("CD4+", "辅助性T细胞数", "CD4阳性T细胞计数"), units=("cells/μL", "/μL", "个/μL"), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_CD8_PERCENT", "CD8阳性T细胞百分比", r"CD8 抑制/细胞毒性T细胞%", ("CD8%", "抑制/细胞毒性T细胞%", "CD8阳性T细胞百分比"), units=("%",), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_CD8_COUNT", "CD8阳性T细胞计数", r"CD8\+ 抑制/细胞毒性T细胞数", ("CD8+", "抑制/细胞毒性T细胞数", "CD8阳性T细胞计数"), units=("cells/μL", "/μL", "个/μL"), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_TOTAL_B_COUNT", "总B淋巴细胞计数", r"T-BLY|总B淋巴细胞$", ("T-BLY", "总B淋巴细胞", "总B淋巴细胞计数"), units=("cells/μL", "/μL", "个/μL"), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_TOTAL_T_COUNT", "总T淋巴细胞计数", r"T-TLY|总T淋巴细胞$", ("T-TLY", "总T淋巴细胞", "总T淋巴细胞计数"), units=("cells/μL", "/μL", "个/μL"), category="IMMUNOLOGY", capability="EXPLORATORY"),
    _lab("LAB_LYMPH_TOTAL_COUNT", "淋巴细胞总数", r"T-LY|淋巴细胞总数", ("T-LY", "淋巴细胞总数"), units=("cells/μL", "/μL", "个/μL"), category="IMMUNOLOGY", capability="EXPLORATORY"),
)


GENE_SYMBOLS = (
    "AKT1", "AKT2", "ALK", "ATM", "ATR", "BARD1", "BRAF", "BRCA1", "BRCA2", "BRIP1",
    "CCDC6", "CCND1", "CDK12", "CDK4", "CHEK1", "CHEK2", "DNMT3A", "EGFR", "ERCC1", "ERCC2",
    "ESR1", "FGF19", "FGFR2", "FGFR3", "IDH1", "KRAS", "MDM2", "MDM4", "MET", "NF1",
    "NTRK1", "NTRK2", "NTRK3", "PALB2", "PDGFRA", "PIK3CA", "PMS2", "POLD1", "POLE", "PTEN",
    "RAD50", "RAD51B", "RAD51C", "RAD51D", "RAD54L", "RET", "ROS1", "STK11", "TERT", "VHL",
)


def _gene(symbol):
    return IndicatorSpec(
        code=f"GENE_{symbol.replace('-', '_')}",
        standard_name=symbol,
        selector_pattern=rf"(^|[^A-Z0-9]){re.escape(symbol)}([^A-Z0-9]|$)",
        aliases=(symbol,),
        category="MOLECULAR_GENE",
        capability_level="SEARCH_ONLY",
    )


INDICATOR_SPECS = CONVENTIONAL_SPECS + tuple(_gene(symbol) for symbol in GENE_SYMBOLS)
