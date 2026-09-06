import re
import unicodedata


_CJK = r"\u3400-\u4dbf\u4e00-\u9fff"
_LEADING_MARKERS = re.compile(
    r"^(?:(?:[\s*•·●▪■□◆◇▶►↑↓★☆△]+)|(?:\d{1,3}\s*(?:[.)、:：]|[★☆*△]+)\s*))+"
)
_TRAILING_FLAGS = re.compile(r"\s*(?:[↑↓]|\b[HL]\b|[+*])\s*$", re.IGNORECASE)
_NUMERIC_RESULT = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[<>≤≥]=?\s*)?[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?:\s*[↑↓HL])?(?=\s|$)",
    re.IGNORECASE,
)
_STATUS_VALUES = (
    "未检出",
    "未报告",
    "未见",
    "阴性",
    "阳性",
    "弱阳性",
    "可疑",
    "溶血",
    "拒收",
    "正常",
    "异常",
    "negative",
    "positive",
)
_STATUS_RESULT = re.compile(r"(?:^|[\s:：])(?:" + "|".join(map(re.escape, _STATUS_VALUES)) + r")(?:\s|$)", re.IGNORECASE)
_EXACT_RESULT = re.compile(
    r"^(?:(?:[<>≤≥]=?\s*)?[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?:\s*[↑↓HL])?|"
    + "|".join(map(re.escape, _STATUS_VALUES))
    + r")(?:\s*[()（）+\-]*)$",
    re.IGNORECASE,
)
_REJECT_KEYWORDS = (
    "姓名",
    "患者",
    "病人",
    "病人类型",
    "患者姓名",
    "病人姓名",
    "受检者",
    "身份证",
    "身份证号",
    "住院号",
    "门诊号",
    "病历号",
    "条码号",
    "样本号",
    "标本号",
    "床号",
    "科室",
    "住院",
    "门诊",
    "医生",
    "医师",
    "性别",
    "年龄",
    "出生日期",
    "联系电话",
    "手机号码",
    "报告日期",
    "采样日期",
    "采样时间",
    "送检时间",
    "审核时间",
    "检验报告",
    "申请单",
    "病史",
    "诊断",
    "治疗",
    "化疗",
    "本检测项目",
)
_HEADER_WORDS = ("项目", "名称", "结果", "单位", "参考", "范围", "提示", "方法")
_PROSE_PUNCTUATION = re.compile(r"[,，。；;！？!?]")


def _clean_text(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = "".join(character for character in value if not unicodedata.category(character).startswith("C"))
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", value)
    value = re.sub(rf"(?<=[{_CJK}])\s+([A-Za-z])\s+(?=[{_CJK}])", r"\1", value)
    return value


def result_token(value):
    value = _clean_text(value)
    return bool(value and _EXACT_RESULT.fullmatch(value))


def normalize_candidate_name(value, *, strip_result=True):
    value = _clean_text(value)
    if not value:
        return ""
    value = _LEADING_MARKERS.sub("", value).strip()
    if strip_result:
        status = _STATUS_RESULT.search(value)
        numeric = _NUMERIC_RESULT.search(value)
        cuts = [match.start() for match in (status, numeric) if match is not None]
        if cuts:
            value = value[: min(cuts)].rstrip(" :：,，;；")
    value = _TRAILING_FLAGS.sub("", value).strip(" :：,，;；|｜")
    value = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", value)
    value = re.sub(rf"(?<=[{_CJK}])\s+([A-Za-z])\s+(?=[{_CJK}])", r"\1", value)
    return value[:160].strip()


def is_rejected_candidate_name(value):
    value = _clean_text(value)
    if not 2 <= len(value) <= 96:
        return True
    lowered = value.casefold()
    if any(keyword.casefold() in lowered for keyword in _REJECT_KEYWORDS):
        return True
    if "医院" in value or value.endswith(("检验科", "化验室", "检测中心", "医学中心")):
        return True
    if _PROSE_PUNCTUATION.search(value):
        return True
    header_hits = sum(word in value for word in _HEADER_WORDS)
    if value in {"项目名称", "检验项目", "检验结果", "参考范围"} or header_hits >= 3:
        return True
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", value) or re.search(r"\d{6,}", value):
        return True
    meaningful = [character for character in value if character.isalpha() or character in "-_()（）"]
    return not meaningful
